# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Finders: lost-and-found with a reward held on PayPal.

case (lost report + reward) -> PayPal capture of reward + $1 fee -> found reports are matched by the AI
-> the finder proves they hold the item, the owner proves it is theirs -> handover code from owner to finder
-> PayPal Payout of the agreed reward to the finder (and a refund of any reduction) -> or, after 14 days with no
handover, a refund of the reward to the owner.

The AI reads, compares, asks and judges. Code moves money, only on: a correct handover code, an expiry, or the owner
cancelling. Each case pays at most one finder, once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from finders.ai import Brain, ItemView, Photo
from finders.db import Database, new_id
from finders.money import fmt, parse_user_amount, show
from finders.paypal import PayPal, PayPalDefinite, PayPalUncertain, captures_of, payout_batch_from_links

FEE_CENTS = 100
EXPIRY_S = 14 * 24 * 3600
MIN_REWARD_CENTS, MAX_REWARD_CENTS = 500, 50_000
MATCH_MIN_SCORE = 55
MAX_CANDIDATES = 12
CODE_ATTEMPTS = 5
FOUND_WINDOW_S = 30 * 24 * 3600  # a found item can match reports lost up to 30 days earlier
CLAIMS_PER_DAY = 3  # more verified claims than this in 24 h by one finder sends payouts to review
SANDBOX_CARD = {
    "number": "4111111111111111",
    "expiry": "2030-12",
    "security_code": "123",
    "name": "Demo Owner",
    "billing_address": {
        "address_line_1": "1 Main St",
        "admin_area_2": "San Jose",
        "admin_area_1": "CA",
        "postal_code": "95131",
        "country_code": "US",
    },
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _when(ts: float) -> str:
    return time.strftime("%a %d %b %Y, %H:%M UTC", time.gmtime(ts))


class Finders:
    def __init__(self, db: Database, paypal: PayPal, brain: Brain, *, clock=time.time) -> None:
        self.db, self.paypal, self.brain, self.clock = db, paypal, brain, clock

    # --- owners ---------------------------------------------------------------------------------
    async def report_lost(
        self,
        *,
        owner_name: str,
        owner_email: str,
        title: str,
        description: str,
        private_details: str,
        place: str,
        lost_at: float,
        reward: str,
        photos: list[Photo] = (),  # type: ignore[assignment]
        sample: bool = False,
    ) -> tuple[str, str]:
        """Returns (case id, owner token). The case is UNPAID until the reward is captured."""
        reward_c = parse_user_amount(reward)
        if not MIN_REWARD_CENTS <= reward_c <= MAX_REWARD_CENTS:
            raise ValueError(f"reward must be between {show(MIN_REWARD_CENTS)} and {show(MAX_REWARD_CENTS)}")
        if len(private_details.strip()) < 3:
            raise ValueError("add at least one private detail only you know (it is never shown)")
        if lost_at > self.clock() + 3600:
            raise ValueError("lost time is in the future")
        attrs = await self.brain.describe(f"{title}. {description}", list(photos))
        cid, token, now = new_id("case"), secrets.token_urlsafe(24), self.clock()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO lost(id, status, owner_name, owner_email, owner_token, title, description, private_details,"
                " place, lost_at, attributes, reward_cents, fee_cents, sample, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    "UNPAID",
                    owner_name.strip()[:60],
                    owner_email.strip().lower()[:254],
                    token_hash(token),
                    title.strip()[:80],
                    description.strip()[:1000],
                    private_details.strip()[:500],
                    place.strip()[:120],
                    lost_at,
                    attrs.model_dump_json(),
                    reward_c,
                    FEE_CENTS,
                    1 if sample else 0,
                    now,
                    now,
                ),
            )
            for p in photos:
                self._add_photo(conn, "lost", cid, p)
            self.db.event(
                "lost_reported", cid, conn=conn, title=title, place=place, reward=fmt(reward_c),
                attributes=attrs.model_dump(), sample=sample,
            )  # fmt: skip
        return cid, token

    async def pay_with_card(self, case_id: str) -> dict[str, Any]:
        """Sandbox test card: PayPal captures at once."""
        case = self._case(case_id)
        if case["status"] != "UNPAID":
            raise RuntimeError(f"case is {case['status']}")
        order = await self.paypal.create_reward_order(
            fmt(case["reward_cents"] + case["fee_cents"]),
            case_id=case_id,
            request_id=f"pay{case_id}",
            description=f"Finders reward: {case['title']}",
            card=SANDBOX_CARD,
        )
        return self._record_payment(case_id, order)

    async def start_wallet_payment(self, case_id: str, return_url: str, cancel_url: str) -> dict[str, Any]:
        case = self._case(case_id)
        if case["status"] != "UNPAID":
            raise RuntimeError(f"case is {case['status']}")
        order = await self.paypal.create_reward_order(
            fmt(case["reward_cents"] + case["fee_cents"]),
            case_id=case_id,
            request_id=f"payw{case_id}",
            description=f"Finders reward: {case['title']}",
            return_url=return_url,
            cancel_url=cancel_url,
        )
        with self.db.tx() as conn:
            conn.execute("UPDATE lost SET order_id=?, updated_at=? WHERE id=?", (order["id"], self.clock(), case_id))
        return {"order_id": order["id"]}

    async def finish_wallet_payment(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        if case["status"] != "UNPAID" or not case["order_id"]:
            raise RuntimeError(f"case is {case['status']}")
        try:
            order = await self.paypal.capture_order(case["order_id"], request_id=f"cap{case_id}")
        except PayPalUncertain:
            order = await self.paypal.get_order(case["order_id"])  # did it go through?
        return self._record_payment(case_id, order)

    def _record_payment(self, case_id: str, order: dict[str, Any]) -> dict[str, Any]:
        case = self._case(case_id)
        caps = [c for c in captures_of(order) if c.get("status") in ("COMPLETED", "PENDING")]
        if not caps:
            raise RuntimeError(f"PayPal did not take the payment (order status {order.get('status')})")
        cap = caps[0]
        due = case["reward_cents"] + case["fee_cents"]
        if cap["amount"]["value"] != fmt(due) or cap["amount"]["currency_code"] != "USD":
            raise RuntimeError("captured amount differs from reward plus fee")
        now = self.clock()
        if cap["status"] == "PENDING":
            # PayPal has the payment but has not completed it: the reward is not live and nobody can be paid from it
            with self.db.tx() as conn:
                cur = conn.execute(
                    "UPDATE lost SET status='PAYMENT_PENDING', order_id=?, capture_id=?, updated_at=? "
                    "WHERE id=? AND status='UNPAID'",
                    (order["id"], cap["id"], now, case_id),
                )
                if cur.rowcount:
                    self.db.event("reward_payment_pending", case_id, conn=conn, capture_id=cap["id"])
            return {"order_id": order["id"], "capture_id": cap["id"], "amount": fmt(due), "pending": True}
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE lost SET status='OPEN', order_id=?, capture_id=?, paid_cents=?, expires_at=?, updated_at=? "
                "WHERE id=? AND status IN ('UNPAID','PAYMENT_PENDING')",
                (order["id"], cap["id"], due, now + EXPIRY_S, now, case_id),
            )
            if cur.rowcount:
                self.db.event(
                    "reward_paid", case_id, conn=conn, order_id=order["id"], capture_id=cap["id"],
                    reward=fmt(case["reward_cents"]), fee=fmt(case["fee_cents"]), expires=_when(now + EXPIRY_S),
                )  # fmt: skip
        return {"order_id": order["id"], "capture_id": cap["id"], "amount": fmt(due)}

    # --- finders --------------------------------------------------------------------------------
    async def report_found(
        self,
        *,
        finder_name: str,
        finder_email: str,
        description: str,
        place: str,
        found_at: float,
        photos: list[Photo],
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Returns (found id, finder token, proposed matches)."""
        email = finder_email.strip().lower()
        if "@" not in email or any(c.isspace() for c in email) or len(email) > 254:
            raise ValueError("a PayPal email is needed to receive a reward")
        if not photos:
            raise ValueError("add a photo of the item")
        attrs = await self.brain.describe(description, photos)
        fid, token, now = new_id("fnd"), secrets.token_urlsafe(24), self.clock()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO found(id, status, finder_name, finder_email, finder_token, description, place, found_at,"
                " attributes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    fid, "OPEN", finder_name.strip()[:60], email, token_hash(token), description.strip()[:1000],
                    place.strip()[:120], found_at, attrs.model_dump_json(), now,
                ),
            )  # fmt: skip
            for p in photos:
                self._add_photo(conn, "found", fid, p)
            self.db.event("found_reported", None, conn=conn, found_id=fid, place=place, attributes=attrs.model_dump())
        matches = await self.match(fid)
        return fid, token, matches

    async def match(self, found_id: str) -> list[dict[str, Any]]:
        """Rules pick plausible open cases (time window); the AI ranks them; strong ones become proposed matches."""
        f = self.db.one("SELECT * FROM found WHERE id=?", found_id)
        cands = self.db.all(
            "SELECT * FROM lost WHERE status='OPEN' AND lost_at <= ? AND lost_at >= ? ORDER BY lost_at DESC LIMIT ?",
            f["found_at"] + 3600,
            f["found_at"] - FOUND_WINDOW_S,
            MAX_CANDIDATES * 3,
        )
        fattrs = json.loads(f["attributes"])
        # cheap rule: drop clearly different item types before asking the model
        same = [
            c for c in cands if _compatible(fattrs.get("category", ""), json.loads(c["attributes"]).get("category", ""))
        ]
        same = same[:MAX_CANDIDATES]
        if not same:
            self.db.event("match_none", None, found_id=found_id, considered=len(cands))
            return []
        ranking = await self.brain.rank(self._found_view(f), [self._lost_view(c) for c in same])
        strong = sorted((c for c in ranking.candidates if c.score >= MATCH_MIN_SCORE), key=lambda c: -c.score)[:3]
        out = []
        now = self.clock()
        with self.db.tx() as conn:
            for c in strong:
                mid = new_id("mch")
                conn.execute(
                    "INSERT OR IGNORE INTO matches(id, lost_id, found_id, score, reasons, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (mid, c.lost_id, found_id, c.score, json.dumps(c.reasons), "PROPOSED", now, now),
                )
                self.db.event(
                    "match_proposed", c.lost_id, conn=conn, match_id=mid, found_id=found_id, score=c.score,
                    reasons=c.reasons,
                )  # fmt: skip
                out.append({"match_id": mid, "lost_id": c.lost_id, "score": c.score, "reasons": c.reasons})
            self.db.event(
                "match_ranked", None, conn=conn, found_id=found_id, considered=len(same), proposed=len(out),
                brain=self.brain.name,
            )  # fmt: skip
        return out

    # --- verification ---------------------------------------------------------------------------
    async def finder_challenge(self, match_id: str, found_id: str) -> dict[str, Any]:
        m = self._match(match_id)
        if m["found_id"] != found_id:
            raise PermissionError("not your match")
        if m["status"] not in ("PROPOSED", "CHECKING"):
            raise RuntimeError(f"match is {m['status']}")
        if not m["finder_question"]:
            case = self._case(m["lost_id"])
            q = await self.brain.finder_question(self._lost_view(case, private=True))
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE matches SET finder_question=?, finder_reason=?, status='CHECKING', updated_at=? WHERE id=?",
                    (q.question, json.dumps({"expected": q.expected}), self.clock(), match_id),
                )
                self.db.event("finder_asked", m["lost_id"], conn=conn, match_id=match_id, question=q.question)
            m = self._match(match_id)
        case = self._case(m["lost_id"])
        return {
            "match_id": match_id,
            "question": m["finder_question"],
            "case": {"title": case["title"], "place": case["place"], "reward": fmt(case["reward_cents"])},
        }

    async def finder_answer(self, match_id: str, found_id: str, answer: str) -> dict[str, Any]:
        m = self._match(match_id)
        if m["found_id"] != found_id:
            raise PermissionError("not your match")
        if m["status"] != "CHECKING" or m["finder_verdict"] is not None:
            raise RuntimeError("this question was already answered")
        expected = json.loads(m["finder_reason"])["expected"]
        v = await self.brain.judge(m["finder_question"], expected, answer)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE matches SET finder_answer=?, finder_verdict=?, finder_reason=?, updated_at=? WHERE id=?",
                (
                    answer[:300],
                    v.verdict,
                    json.dumps({"expected": expected, "reason": v.reason}),
                    self.clock(),
                    match_id,
                ),
            )
            if v.verdict == "no":
                conn.execute("UPDATE matches SET status='REJECTED' WHERE id=?", (match_id,))
            elif v.verdict == "unsure":
                conn.execute("UPDATE matches SET status='REVIEW' WHERE id=?", (match_id,))
            self.db.event(
                "finder_checked", m["lost_id"], conn=conn, match_id=match_id, verdict=v.verdict, reason=v.reason
            )
        if v.verdict == "yes":
            await self._ask_owner(match_id)
        return {"verdict": v.verdict, "reason": v.reason}

    async def _ask_owner(self, match_id: str) -> None:
        m = self._match(match_id)
        case = self._case(m["lost_id"])
        f = self.db.one("SELECT * FROM found WHERE id=?", m["found_id"])
        q = await self.brain.owner_question(
            self._found_view(f), self._lost_view(case), avoid=m["finder_question"] or ""
        )
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE matches SET owner_question=?, owner_reason=?, updated_at=? WHERE id=?",
                (q.question, json.dumps({"expected": q.expected}), self.clock(), match_id),
            )
            self.db.event("owner_asked", m["lost_id"], conn=conn, match_id=match_id, question=q.question)

    async def owner_answer(self, case_id: str, match_id: str, answer: str) -> dict[str, Any]:
        m = self._match(match_id)
        if m["lost_id"] != case_id:
            raise PermissionError("not your case")
        if m["status"] != "CHECKING" or not m["owner_question"] or m["owner_verdict"] is not None:
            raise RuntimeError("no open question for you on this match")
        expected = json.loads(m["owner_reason"])["expected"]
        v = await self.brain.judge(m["owner_question"], expected, answer)
        flags = self._flags(m)
        # A wrong owner answer goes to a person rather than rejecting outright: the owner already paid the reward, and
        # real owners forget details. A wrong FINDER answer is rejected (that is the reward-farming risk).
        status = {"yes": "VERIFIED", "no": "REVIEW", "unsure": "REVIEW"}[v.verdict]
        if status == "VERIFIED" and flags:
            status = "REVIEW"
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE matches SET owner_answer=?, owner_verdict=?, owner_reason=?, status=?, flags=?, updated_at=? "
                "WHERE id=?",
                (
                    answer[:300], v.verdict, json.dumps({"expected": expected, "reason": v.reason}), status,
                    json.dumps(flags), self.clock(), match_id,
                ),
            )  # fmt: skip
            if status == "VERIFIED":
                conn.execute("UPDATE lost SET status='MATCHED', updated_at=? WHERE id=?", (self.clock(), case_id))
                conn.execute("UPDATE found SET status='MATCHED' WHERE id=?", (m["found_id"],))
            self.db.event(
                "owner_checked", case_id, conn=conn, match_id=match_id, verdict=v.verdict, reason=v.reason,
                flags=flags, status=status,
            )  # fmt: skip
        return {"verdict": v.verdict, "reason": v.reason, "status": status, "flags": flags}

    def review(self, match_id: str, approve: bool, note: str = "") -> dict[str, Any]:
        """A person decides a match the AI was unsure about or that tripped a fraud rule."""
        m = self._match(match_id)
        if m["status"] != "REVIEW":
            raise RuntimeError(f"match is {m['status']}")
        if approve and not (m["finder_verdict"] and m["owner_question"]):
            # finder answer was unsure: approving it moves on to the owner's question
            status = "CHECKING"
        else:
            status = "VERIFIED" if approve else "REJECTED"
        with self.db.tx() as conn:
            conn.execute("UPDATE matches SET status=?, updated_at=? WHERE id=?", (status, self.clock(), match_id))
            if status == "VERIFIED":
                conn.execute("UPDATE lost SET status='MATCHED', updated_at=? WHERE id=?", (self.clock(), m["lost_id"]))
                conn.execute("UPDATE found SET status='MATCHED' WHERE id=?", (m["found_id"],))
            self.db.event(
                "match_reviewed", m["lost_id"], conn=conn, match_id=match_id, approve=approve, note=note[:200]
            )
        return {"status": status}

    # --- handover & money -----------------------------------------------------------------------
    def handover_code(self, case_id: str, match_id: str, reward: str | None = None) -> dict[str, Any]:
        """Owner: get the code to give the finder once the item is in their hands. A lower reward may be set here."""
        m = self._match(match_id)
        case = self._case(case_id)
        if m["lost_id"] != case_id or m["status"] != "VERIFIED" or case["status"] != "MATCHED":
            raise RuntimeError("no verified match for this case")
        agreed = case["reward_cents"] if reward is None else parse_user_amount(reward)
        if not 0 < agreed <= case["reward_cents"]:
            raise ValueError("the reward can be reduced, not increased or set to zero")
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE matches SET code_hash=?, code_attempts=0, reward_cents=?, updated_at=? WHERE id=?",
                (token_hash(f"{match_id}:{code}"), agreed, self.clock(), match_id),
            )
            self.db.event("handover_code_issued", case_id, conn=conn, match_id=match_id, reward=fmt(agreed))
        return {"code": code, "reward": fmt(agreed)}

    async def claim(self, found_id: str, match_id: str, code: str) -> dict[str, Any]:
        """Finder: enter the owner's code. A correct code is the owner's consent: the reward is paid out."""
        m = self._match(match_id)
        if m["found_id"] != found_id:
            raise PermissionError("not your match")
        if m["status"] != "VERIFIED" or not m["code_hash"]:
            raise RuntimeError("the owner has not issued a handover code yet")
        if m["code_attempts"] >= CODE_ATTEMPTS:
            raise RuntimeError("too many wrong codes; ask the owner for a new one")
        ok = hmac.compare_digest(token_hash(f"{match_id}:{code.strip()}"), m["code_hash"])
        with self.db.tx() as conn:
            if not ok:
                conn.execute("UPDATE matches SET code_attempts=code_attempts+1 WHERE id=?", (match_id,))
                self.db.event("handover_code_wrong", m["lost_id"], conn=conn, match_id=match_id)
            else:
                cur = conn.execute(
                    "UPDATE matches SET status='HANDED_OVER', updated_at=? WHERE id=? AND status='VERIFIED'",
                    (self.clock(), match_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("already handed over")
                conn.execute("UPDATE lost SET status='RETURNED', updated_at=? WHERE id=?", (self.clock(), m["lost_id"]))
                conn.execute("UPDATE found SET status='RETURNED' WHERE id=?", (found_id,))
                self._prepare_settlements(conn, m)
                self.db.event("handed_over", m["lost_id"], conn=conn, match_id=match_id, reward=fmt(m["reward_cents"]))
        if not ok:
            raise ValueError("wrong code")
        await self.settle(m["lost_id"])
        return self.case_money(m["lost_id"])

    def _prepare_settlements(self, conn: Any, m: Any) -> None:
        case = conn.execute("SELECT * FROM lost WHERE id=?", (m["lost_id"],)).fetchone()
        f = conn.execute("SELECT * FROM found WHERE id=?", (m["found_id"],)).fetchone()
        agreed = m["reward_cents"]
        if agreed + case["paid_out_cents"] + case["refunded_cents"] > case["reward_cents"]:
            raise RuntimeError("payout would exceed the reward")
        now = self.clock()
        conn.execute(
            "INSERT INTO settlements(id, lost_id, match_id, kind, amount_cents, receiver, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id("pay"), case["id"], m["id"], "payout", agreed, f["finder_email"], "SENDING", now, now),
        )
        if agreed < case["reward_cents"]:
            conn.execute(
                "INSERT INTO settlements(id, lost_id, match_id, kind, amount_cents, receiver, state, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id("ref"), case["id"], m["id"], "refund", case["reward_cents"] - agreed, case["owner_email"],
                 "SENDING", now, now),
            )  # fmt: skip

    async def cancel(self, case_id: str, reason: str = "owner cancelled") -> dict[str, Any]:
        """Owner found it themselves, or the case expired: refund the reward (the $1 fee is kept)."""
        case = self._case(case_id)
        if case["status"] not in ("OPEN", "MATCHED"):
            raise RuntimeError(f"case is {case['status']}")
        if self.db.one("SELECT 1 FROM matches WHERE lost_id=? AND status='HANDED_OVER'", case_id):
            raise RuntimeError("the item was already handed over")
        new_status = "EXPIRED" if reason == "expired" else "CANCELLED"
        now = self.clock()
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE lost SET status=?, updated_at=? WHERE id=? AND status IN ('OPEN','MATCHED')",
                (new_status, now, case_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("case changed; try again")
            conn.execute(
                "UPDATE matches SET status='REJECTED', updated_at=? WHERE lost_id=? AND status IN "
                "('PROPOSED','CHECKING','REVIEW','VERIFIED')",
                (now, case_id),
            )
            conn.execute(
                "INSERT INTO settlements(id, lost_id, kind, amount_cents, receiver, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (new_id("ref"), case_id, "refund", case["reward_cents"], case["owner_email"], "SENDING", now, now),
            )
            self.db.event("case_closed", case_id, conn=conn, reason=reason, refund=fmt(case["reward_cents"]))
        await self.settle(case_id)
        return self.case_money(case_id)

    async def check_pending_payments(self) -> None:
        """Owner payments PayPal reported as PENDING: open the case once COMPLETED, close it if declined."""
        for c in self.db.all("SELECT * FROM lost WHERE status='PAYMENT_PENDING' AND capture_id IS NOT NULL"):
            try:
                cap = await self.paypal.get_capture(c["capture_id"])
            except (PayPalUncertain, PayPalDefinite):
                continue
            if cap.get("status") == "COMPLETED":
                order = {"id": c["order_id"], "status": "COMPLETED",
                         "purchase_units": [{"payments": {"captures": [cap]}}]}  # fmt: skip
                self._record_payment(c["id"], order)
            elif cap.get("status") in ("DECLINED", "FAILED"):
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE lost SET status='PAYMENT_FAILED', updated_at=? WHERE id=?", (self.clock(), c["id"])
                    )
                    self.db.event("reward_payment_failed", c["id"], conn=conn, status=cap.get("status"))

    async def expire_due(self) -> list[str]:
        due = self.db.all(
            "SELECT id FROM lost WHERE status IN ('OPEN','MATCHED') AND expires_at IS NOT NULL AND expires_at < ?",
            self.clock(),
        )
        done = []
        for r in due:
            try:
                await self.cancel(r["id"], reason="expired")
                done.append(r["id"])
            except RuntimeError:
                continue
        return done

    async def settle(self, case_id: str | None = None) -> None:
        """Send every unsent or uncertain payout/refund. Safe to repeat: ids are fixed when the row is written."""
        rows = self.db.all(
            "SELECT * FROM settlements WHERE state IN ('SENDING','UNCERTAIN') " + ("AND lost_id=? " if case_id else "")
            + "ORDER BY created_at",
            *([case_id] if case_id else []),
        )  # fmt: skip
        for s in rows:
            if s["kind"] == "payout":
                await self._payout(s)
            else:
                await self._refund(s)

    async def _payout(self, s: Any) -> None:
        case = self._case(s["lost_id"])
        if not case["capture_id"]:
            self._set(s["id"], state="FAILED", error="reward was never captured")
            return
        item = {
            "recipient_type": "EMAIL",
            "receiver": s["receiver"],
            "amount": {"value": fmt(s["amount_cents"]), "currency": "USD"},
            "note": f"Finders reward for returning: {case['title']}"[:200],
            "sender_item_id": s["id"],
        }
        try:
            res = await self.paypal.create_payout(
                sender_batch_id=s["id"],
                items=[item],
                subject="You have a reward from Finders",
                message=f"Thank you for returning '{case['title']}'. Here is the reward the owner offered.",
            )
        except PayPalUncertain as exc:
            self._set(s["id"], state="UNCERTAIN", error=str(exc))
            self.db.event("payout_uncertain", s["lost_id"], settlement_id=s["id"])
            return
        except PayPalDefinite as exc:
            if any("already exists" in m for m in (exc.issues + [str(exc)])):
                # our earlier attempt reached PayPal: never pay twice; PayPal's error links the original batch
                batch = payout_batch_from_links(exc.links)
                if batch:
                    self._set(s["id"], state="SUBMITTED", paypal_id=batch, error=None)
                    self._money(s, "paid_out_cents")
                    self.db.event(
                        "payout_already_submitted", s["lost_id"], settlement_id=s["id"], payout_batch_id=batch
                    )
                else:
                    self._set(s["id"], state="REVIEW", error="duplicate batch but original batch id not returned")
                    self.db.event("payout_needs_review", s["lost_id"], settlement_id=s["id"])
                return
            self._set(s["id"], state="FAILED", error=f"{exc.name} {exc.issues}")
            self.db.event("payout_refused", s["lost_id"], settlement_id=s["id"], name=exc.name, issues=exc.issues)
            return
        batch = res.get("batch_header", {}).get("payout_batch_id")
        self._set(s["id"], state="SUBMITTED", paypal_id=batch, error=None)
        self._money(s, "paid_out_cents")
        self.db.event(
            "payout_submitted", s["lost_id"], settlement_id=s["id"], payout_batch_id=batch,
            amount=fmt(s["amount_cents"]), receiver=_mask(s["receiver"]),
        )  # fmt: skip

    async def _refund(self, s: Any) -> None:
        case = self._case(s["lost_id"])
        try:
            res = await self.paypal.refund_capture(
                case["capture_id"],
                amount=fmt(s["amount_cents"]),
                request_id=s["id"],
                custom_id=s["id"],
                note=f"Finders: reward refund for '{case['title']}'",
            )
        except PayPalUncertain as exc:
            self._set(s["id"], state="UNCERTAIN", error=str(exc))
            self.db.event("refund_uncertain", s["lost_id"], settlement_id=s["id"])
            return
        except PayPalDefinite as exc:
            self._set(s["id"], state="FAILED", error=f"{exc.name} {exc.issues}")
            self.db.event("refund_refused", s["lost_id"], settlement_id=s["id"], name=exc.name, issues=exc.issues)
            return
        self._record_refund(s, res)

    def _record_refund(self, s: Any, res: dict[str, Any]) -> None:
        status = res.get("status")
        if res.get("amount", {}).get("value") != fmt(s["amount_cents"]):
            self._set(s["id"], state="REVIEW", paypal_id=res.get("id"), error="refund amount differs")
            return
        if status == "PENDING":
            self._set(s["id"], state="PENDING", paypal_id=res.get("id"), item_status=status, error=None)
            return
        if status in ("FAILED", "CANCELLED"):
            self._set(s["id"], state="FAILED", paypal_id=res.get("id"), item_status=status)
            self.db.event("refund_refused", s["lost_id"], settlement_id=s["id"], name=status, issues=[])
            return
        if status != "COMPLETED":
            self._set(s["id"], state="UNCERTAIN", error=f"unrecognised refund status {status}")
            return
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE settlements SET state='DONE', paypal_id=?, item_status=?, error=NULL, updated_at=? "
                "WHERE id=? AND state!='DONE'",
                (res.get("id"), status, self.clock(), s["id"]),
            )
            if not cur.rowcount:
                return
            conn.execute(
                "UPDATE lost SET refunded_cents=refunded_cents+?, updated_at=? WHERE id=?",
                (s["amount_cents"], self.clock(), s["lost_id"]),
            )
        self.db.event(
            "refunded", s["lost_id"], settlement_id=s["id"], refund_id=res.get("id"), amount=fmt(s["amount_cents"])
        )

    async def refresh_refunds(self, case_id: str | None = None) -> None:
        rows = self.db.all(
            "SELECT * FROM settlements WHERE kind='refund' AND state='PENDING' AND paypal_id IS NOT NULL"
            + (" AND lost_id=?" if case_id else ""),
            *([case_id] if case_id else []),
        )
        for s in rows:
            try:
                res = await self.paypal.get_refund(s["paypal_id"])
            except (PayPalUncertain, PayPalDefinite):
                continue
            self._record_refund(s, res)

    async def refresh_payouts(self, case_id: str | None = None) -> None:
        """A batch can succeed while the person's item is still UNCLAIMED or pending: track the item itself."""
        rows = self.db.all(
            "SELECT * FROM settlements WHERE kind='payout' AND state IN ('SUBMITTED','ITEMS_PENDING') "
            "AND paypal_id IS NOT NULL" + (" AND lost_id=?" if case_id else ""),
            *([case_id] if case_id else []),
        )
        for s in rows:
            try:
                res = await self.paypal.get_payout(s["paypal_id"])
            except (PayPalUncertain, PayPalDefinite):
                continue
            items = res.get("items", [])
            st = items[0].get("transaction_status") if items else None
            batch = res.get("batch_header", {}).get("batch_status")
            state = "DONE" if st == "SUCCESS" else "FAILED" if st in ("FAILED", "RETURNED", "BLOCKED", "REFUNDED") \
                or batch in ("DENIED", "CANCELED") else "ITEMS_PENDING"  # fmt: skip
            if state != s["state"] or st != s["item_status"]:
                self._set(s["id"], state=state, item_status=st)
                self.db.event("payout_checked", s["lost_id"], settlement_id=s["id"], item_status=st, state=state)

    def _money(self, s: Any, column: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE lost SET {column}={column}+?, updated_at=? WHERE id=?",
                (s["amount_cents"], self.clock(), s["lost_id"]),
            )

    # --- views ----------------------------------------------------------------------------------
    def owner_for_token(self, token: str) -> str | None:
        r = self.db.one("SELECT id FROM lost WHERE owner_token=?", token_hash(token)) if token else None
        return r["id"] if r else None

    def finder_for_token(self, token: str) -> str | None:
        r = self.db.one("SELECT id FROM found WHERE finder_token=?", token_hash(token)) if token else None
        return r["id"] if r else None

    def owner_view(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        matches = []
        for m in self.db.all("SELECT * FROM matches WHERE lost_id=? ORDER BY score DESC", case_id):
            f = self.db.one("SELECT * FROM found WHERE id=?", m["found_id"])
            photo = self.db.one("SELECT id FROM photos WHERE owner_kind='found' AND owner_id=? LIMIT 1", f["id"])
            show_detail = m["finder_verdict"] == "yes"  # the owner sees the find only after the finder proved it
            matches.append(
                {
                    "match_id": m["id"],
                    "status": m["status"],
                    "score": m["score"],
                    "reasons": json.loads(m["reasons"]),
                    "found_place": f["place"],
                    "found_at": f["found_at"],
                    "finder_name": f["finder_name"].split()[0] if show_detail else None,
                    "photo_id": photo["id"] if (photo and show_detail) else None,
                    "finder_proved": m["finder_verdict"] == "yes",
                    "owner_question": m["owner_question"] if m["owner_verdict"] is None else None,
                    "owner_verdict": m["owner_verdict"],
                    "reward": fmt(m["reward_cents"]) if m["reward_cents"] else None,
                    "code_issued": bool(m["code_hash"]),
                }
            )
        return {"case": self._public_case(case), "matches": matches, "money": self.case_money(case_id)}

    def finder_view(self, found_id: str) -> dict[str, Any]:
        f = self.db.one("SELECT * FROM found WHERE id=?", found_id)
        out = []
        for m in self.db.all("SELECT * FROM matches WHERE found_id=? ORDER BY score DESC", found_id):
            case = self._case(m["lost_id"])
            pay = self.db.one("SELECT * FROM settlements WHERE match_id=? AND kind='payout'", m["id"])
            out.append(
                {
                    "match_id": m["id"],
                    "status": m["status"],
                    "score": m["score"],
                    "title": case["title"],
                    "lost_place": case["place"],
                    "reward": fmt(m["reward_cents"] or case["reward_cents"]),
                    "question": m["finder_question"] if m["finder_verdict"] is None else None,
                    "finder_verdict": m["finder_verdict"],
                    "waiting_for_owner": m["finder_verdict"] == "yes" and m["status"] == "CHECKING",
                    "can_claim": m["status"] == "VERIFIED" and bool(m["code_hash"]),
                    "payout": None if pay is None else {"state": pay["state"], "item_status": pay["item_status"],
                                                         "amount": fmt(pay["amount_cents"]), "batch": pay["paypal_id"]},
                }
            )  # fmt: skip
        return {
            "found": {"id": f["id"], "status": f["status"], "place": f["place"], "description": f["description"],
                      "attributes": json.loads(f["attributes"]), "finder_name": f["finder_name"]},
            "matches": out,
        }  # fmt: skip

    def case_money(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        rows = self.db.all("SELECT * FROM settlements WHERE lost_id=? ORDER BY created_at", case_id)
        return {
            "reward": fmt(case["reward_cents"]),
            "fee": fmt(case["fee_cents"]),
            "paid": fmt(case["paid_cents"]),
            "paid_out": fmt(case["paid_out_cents"]),
            "refunded": fmt(case["refunded_cents"]),
            "held": fmt(max(0, case["paid_cents"] - case["fee_cents"] - case["paid_out_cents"] - case["refunded_cents"])),
            "capture_id": case["capture_id"],
            "order_id": case["order_id"],
            "settlements": [
                {"id": s["id"], "kind": s["kind"], "amount": fmt(s["amount_cents"]), "state": s["state"],
                 "paypal_id": s["paypal_id"], "item_status": s["item_status"], "error": s["error"]}
                for s in rows
            ],
        }  # fmt: skip

    def cases(self) -> list[dict[str, Any]]:
        out = []
        for c in self.db.all("SELECT * FROM lost ORDER BY created_at DESC"):
            n = self.db.one("SELECT COUNT(*) n FROM matches WHERE lost_id=?", c["id"])["n"]
            out.append(
                {**self._public_case(c), "matches": n, **self.case_money(c["id"]), "owner_name": c["owner_name"]}
            )
        return out

    def found_items(self) -> list[dict[str, Any]]:
        return [
            {"id": f["id"], "status": f["status"], "place": f["place"], "description": f["description"],
             "finder_name": f["finder_name"], "found_at": f["found_at"], "attributes": json.loads(f["attributes"])}
            for f in self.db.all("SELECT * FROM found ORDER BY created_at DESC")
        ]  # fmt: skip

    def all_matches(self) -> list[dict[str, Any]]:
        out = []
        for m in self.db.all("SELECT * FROM matches ORDER BY created_at DESC"):
            out.append(
                {
                    "id": m["id"], "lost_id": m["lost_id"], "found_id": m["found_id"], "score": m["score"],
                    "status": m["status"], "reasons": json.loads(m["reasons"]), "flags": json.loads(m["flags"]),
                    "finder_question": m["finder_question"], "finder_verdict": m["finder_verdict"],
                    "owner_question": m["owner_question"], "owner_verdict": m["owner_verdict"],
                    "reward": fmt(m["reward_cents"]) if m["reward_cents"] else None,
                }
            )  # fmt: skip
        return out

    def photo(self, photo_id: str) -> tuple[str, bytes] | None:
        r = self.db.one("SELECT mime, data FROM photos WHERE id=?", photo_id)
        return (r["mime"], bytes(r["data"])) if r else None

    def events(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {"seq": e["seq"], "at": e["at"], "case_id": e["case_id"], "kind": e["kind"], "data": json.loads(e["data"])}
            for e in self.db.all("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", after, limit)
        ]

    # --- internals ------------------------------------------------------------------------------
    def _public_case(self, c: Any) -> dict[str, Any]:
        photo = self.db.one("SELECT id FROM photos WHERE owner_kind='lost' AND owner_id=? LIMIT 1", c["id"])
        return {
            "id": c["id"], "status": c["status"], "title": c["title"], "description": c["description"],
            "place": c["place"], "lost_at": c["lost_at"], "reward": fmt(c["reward_cents"]), "fee": fmt(c["fee_cents"]),
            "attributes": json.loads(c["attributes"]), "expires_at": c["expires_at"], "sample": bool(c["sample"]),
            "photo_id": photo["id"] if photo else None,
        }  # fmt: skip

    def _flags(self, m: Any) -> list[str]:
        f = self.db.one("SELECT * FROM found WHERE id=?", m["found_id"])
        flags = []
        recent = self.db.one(
            "SELECT COUNT(*) n FROM matches mt JOIN found fd ON fd.id=mt.found_id WHERE fd.finder_email=? "
            "AND mt.status IN ('VERIFIED','HANDED_OVER') AND mt.updated_at > ?",
            f["finder_email"],
            self.clock() - 86400,
        )["n"]
        if recent >= CLAIMS_PER_DAY:
            flags.append(f"finder has {recent} verified claims in the last 24 hours")
        dup = self.db.one(
            "SELECT COUNT(*) n FROM photos p JOIN photos q ON q.sha=p.sha AND q.owner_id!=p.owner_id "
            "WHERE p.owner_kind='found' AND p.owner_id=? AND q.owner_kind='found'",
            f["id"],
        )["n"]
        if dup:
            flags.append("the same photo was used in another found report")
        return flags

    def _lost_view(self, c: Any, private: bool = False) -> ItemView:
        photos = [
            Photo(r["mime"], bytes(r["data"]))
            for r in self.db.all("SELECT mime, data FROM photos WHERE owner_kind='lost' AND owner_id=?", c["id"])
        ]
        return ItemView(
            id=c["id"], text=f"{c['title']}. {c['description']}", place=c["place"], when=_when(c["lost_at"]),
            attributes=json.loads(c["attributes"]), photos=photos, private=c["private_details"] if private else "",
        )  # fmt: skip

    def _found_view(self, f: Any) -> ItemView:
        photos = [
            Photo(r["mime"], bytes(r["data"]))
            for r in self.db.all("SELECT mime, data FROM photos WHERE owner_kind='found' AND owner_id=?", f["id"])
        ]
        return ItemView(
            id=f["id"], text=f["description"], place=f["place"], when=_when(f["found_at"]),
            attributes=json.loads(f["attributes"]), photos=photos,
        )  # fmt: skip

    def _add_photo(self, conn: Any, kind: str, owner_id: str, p: Photo) -> None:
        if p.mime not in ("image/jpeg", "image/png", "image/webp") or len(p.data) > 8 * 1024 * 1024:
            raise ValueError("photos must be JPEG, PNG or WebP, up to 8 MB")
        conn.execute(
            "INSERT INTO photos(id, owner_kind, owner_id, mime, sha, data, created_at) VALUES (?,?,?,?,?,?,?)",
            (new_id("img"), kind, owner_id, p.mime, hashlib.sha256(p.data).hexdigest(), p.data, self.clock()),
        )

    def _case(self, case_id: str) -> Any:
        r = self.db.one("SELECT * FROM lost WHERE id=?", case_id)
        if r is None:
            raise KeyError(case_id)
        return r

    def _match(self, match_id: str) -> Any:
        r = self.db.one("SELECT * FROM matches WHERE id=?", match_id)
        if r is None:
            raise KeyError(match_id)
        return r

    def _set(self, sid: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE settlements SET {cols}, updated_at=? WHERE id=?", (*fields.values(), self.clock(), sid)
            )


def _compatible(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b or a in ("item", "other") or b in ("item", "other"):
        return True
    groups = [{"earbuds", "airpods", "headphones", "earphones"}, {"wallet", "purse", "card holder"},
              {"bag", "backpack", "handbag"}, {"keys", "key", "keychain"}, {"phone", "smartphone", "mobile"}]  # fmt: skip
    return a == b or a in b or b in a or any(a in g and b in g for g in groups)


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}"
