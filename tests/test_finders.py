# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Finders money and verification tests against the LOCAL FAKE PayPal and a scripted AI (no model calls)."""

from __future__ import annotations

import asyncio
import time

import pytest
from fake_paypal import FakePayPal

from finders.ai import Attributes, Candidate, Photo, Question, Ranking, Verdict, leaks
from finders.db import Database
from finders.paypal import PayPal, PayPalDefinite
from finders.service import EXPIRY_S, Finders

PHOTO = Photo("image/jpeg", b"\xff\xd8 fake wallet photo")


def run(c):
    return asyncio.run(c)


class ScriptedBrain:
    """Deterministic AI stand-in: category = first known noun; score set per test; judge = exact keyword match."""

    name = "scripted"

    def __init__(self) -> None:
        self.scores: dict[str, int] = {}
        self.default_score = 80

    async def describe(self, text, photos):
        t = text.lower()
        cat = next((c for c in ("wallet", "keys", "earbuds", "umbrella") if c in t), "item")
        return Attributes(category=cat, summary=text[:60])

    async def rank(self, found, lost):
        return Ranking(
            candidates=[
                Candidate(lost_id=x.id, score=self.scores.get(x.id, self.default_score), reasons=["r"]) for x in lost
            ]
        )

    async def finder_question(self, lost):
        return Question(question="Whose photo is inside?", expected=lost.private)

    async def owner_question(self, found, lost, avoid=""):
        return Question(question="What colour is the keyring?", expected="red")

    async def judge(self, question, expected, answer):
        a = answer.lower()
        if a == "maybe":
            return Verdict(verdict="unsure", reason="ambiguous")
        ok = any(w in a for w in expected.lower().split() if len(w) >= 3)
        return Verdict(verdict="yes" if ok else "no", reason="checked")


class Env:
    def __init__(self, tmp_path) -> None:
        self.fake = FakePayPal()
        self.now = [time.time()]
        self.brain = ScriptedBrain()
        self.path = tmp_path / "f.db"
        self.app = self._make()

    def _make(self) -> Finders:
        return Finders(
            Database(self.path), PayPal("id", "secret", transport=self.fake.transport), self.brain,
            clock=lambda: self.now[0],
        )  # fmt: skip

    def restart(self) -> Finders:
        self.app = self._make()
        return self.app

    def lost(self, title="Brown leather wallet", reward="40.00", private="photo of my daughter Asha", pay=True):
        cid, tok = run(
            self.app.report_lost(
                owner_name="Dev", owner_email="dev@x.test", title=title, description="Lost on bus 500D",
                private_details=private, place="Bus 500D, Silk Board", lost_at=self.now[0] - 3600, reward=reward,
            )
        )  # fmt: skip
        if pay:
            run(self.app.pay_with_card(cid))
        return cid, tok

    def found(self, desc="Brown wallet found on a bus seat", email="conductor@x.test", photo=PHOTO):
        return run(
            self.app.report_found(
                finder_name="Ravi K", finder_email=email, description=desc, place="Bus 500D depot",
                found_at=self.now[0], photos=[photo],
            )
        )  # fmt: skip

    def verify(self, cid, fid, mid, finder="Asha", owner="red"):
        run(self.app.finder_challenge(mid, fid))
        run(self.app.finder_answer(mid, fid, finder))
        return run(self.app.owner_answer(cid, mid, owner))


def happy_path(env, reward=None):
    cid, _ = env.lost()
    fid, _, matches = env.found()
    mid = matches[0]["match_id"]
    assert env.verify(cid, fid, mid)["status"] == "VERIFIED"
    code = env.app.handover_code(cid, mid, reward)["code"]
    return cid, fid, mid, code


def test_reward_is_captured_with_fee_up_front(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost(reward="40.00")
    m = env.app.case_money(cid)
    assert m["paid"] == "41.00" and m["fee"] == "1.00" and m["held"] == "40.00"
    assert env.app._case(cid)["status"] == "OPEN"


def test_full_return_pays_the_finder_once(tmp_path):
    env = Env(tmp_path)
    cid, fid, mid, code = happy_path(env)
    out = run(env.app.claim(fid, mid, code))
    assert env.fake.paid_to("conductor@x.test") == 4000
    assert out["paid_out"] == "40.00" and out["held"] == "0.00" and out["refunded"] == "0.00"
    with pytest.raises(RuntimeError):
        run(env.app.claim(fid, mid, code))  # a second claim pays nothing
    run(env.app.settle(cid))
    assert len(env.fake.payouts) == 1
    assert env.app._case(cid)["status"] == "RETURNED"


def test_reduced_reward_pays_part_and_refunds_the_rest(tmp_path):
    env = Env(tmp_path)
    _cid, fid, mid, code = happy_path(env, reward="25.00")
    out = run(env.app.claim(fid, mid, code))
    assert env.fake.paid_to("conductor@x.test") == 2500
    assert env.fake.refunded_total() == 1500
    assert out["paid_out"] == "25.00" and out["refunded"] == "15.00" and out["held"] == "0.00"


def test_reward_cannot_be_raised_or_zero(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    fid, _, matches = env.found()
    mid = matches[0]["match_id"]
    env.verify(cid, fid, mid)
    for bad in ("41.00", "0.00"):
        with pytest.raises(ValueError):
            env.app.handover_code(cid, mid, bad)


def test_wrong_codes_lock_after_five_tries(tmp_path):
    env = Env(tmp_path)
    cid, fid, mid, code = happy_path(env)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(ValueError):
            run(env.app.claim(fid, mid, wrong))
    with pytest.raises(RuntimeError, match="too many"):
        run(env.app.claim(fid, mid, code))
    assert not env.fake.payouts
    new = env.app.handover_code(cid, mid)["code"]  # the owner can issue a fresh code
    run(env.app.claim(fid, mid, new))
    assert env.fake.paid_to("conductor@x.test") == 4000


def test_fake_finder_fails_the_question_and_gets_nothing(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    fid, _, matches = env.found()
    mid = matches[0]["match_id"]
    run(env.app.finder_challenge(mid, fid))
    assert run(env.app.finder_answer(mid, fid, "a picture of a dog"))["verdict"] == "no"
    assert env.app._match(mid)["status"] == "REJECTED"
    with pytest.raises(RuntimeError):
        env.app.handover_code(cid, mid)
    with pytest.raises(RuntimeError):
        run(env.app.finder_answer(mid, fid, "Asha"))  # no second try on the same match


def test_wrong_owner_answer_goes_to_review_and_nothing_is_paid(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    fid, _, matches = env.found()
    mid = matches[0]["match_id"]
    assert env.verify(cid, fid, mid, owner="blue")["status"] == "REVIEW"
    assert env.app._case(cid)["status"] == "OPEN"
    with pytest.raises(RuntimeError):
        env.app.handover_code(cid, mid)
    env.app.review(mid, approve=False)
    assert env.app._match(mid)["status"] == "REJECTED" and not env.fake.payouts


def test_owner_cannot_answer_before_the_finder_proved_it(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    _fid, _, matches = env.found()
    mid = matches[0]["match_id"]
    with pytest.raises(RuntimeError):
        run(env.app.owner_answer(cid, mid, "red"))


def test_unsure_answer_goes_to_a_person(tmp_path):
    env = Env(tmp_path)
    _cid, _ = env.lost()
    fid, _, matches = env.found()
    mid = matches[0]["match_id"]
    run(env.app.finder_challenge(mid, fid))
    assert run(env.app.finder_answer(mid, fid, "maybe"))["verdict"] == "unsure"
    assert env.app._match(mid)["status"] == "REVIEW"
    env.app.review(mid, approve=True)
    assert env.app._match(mid)["status"] == "CHECKING"


def test_others_cannot_act_on_a_match(tmp_path):
    env = Env(tmp_path)
    _cid, _fid, mid, code = happy_path(env)
    other_cid, _ = env.lost(title="Black umbrella")
    other_fid, _, _ = env.found(desc="Black umbrella at the stop", email="other@x.test")
    with pytest.raises(PermissionError):
        run(env.app.claim(other_fid, mid, code))
    with pytest.raises(PermissionError):
        run(env.app.owner_answer(other_cid, mid, "red"))


def test_no_handover_refunds_the_reward_after_14_days(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    env.now[0] += EXPIRY_S + 60
    assert run(env.app.expire_due()) == [cid]
    m = env.app.case_money(cid)
    assert m["refunded"] == "40.00" and m["held"] == "0.00"
    assert env.fake.refunded_total() == 4000 and not env.fake.payouts
    assert env.app._case(cid)["status"] == "EXPIRED"


def test_owner_cancel_refunds_reward_and_keeps_fee(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    m = run(env.app.cancel(cid))
    assert m["refunded"] == "40.00" and m["paid"] == "41.00"
    with pytest.raises(RuntimeError):
        run(env.app.cancel(cid))
    assert env.fake.refunded_total() == 4000


def test_cancel_after_handover_is_refused(tmp_path):
    env = Env(tmp_path)
    cid, fid, mid, code = happy_path(env)
    run(env.app.claim(fid, mid, code))
    with pytest.raises(RuntimeError):
        run(env.app.cancel(cid))


def test_lost_payout_response_is_never_paid_twice(tmp_path):
    env = Env(tmp_path)
    cid, fid, mid, code = happy_path(env)
    env.fake.fault("payout", "lose_response")
    run(env.app.claim(fid, mid, code))
    s = env.app.db.one("SELECT * FROM settlements WHERE kind='payout'")
    assert s["state"] == "UNCERTAIN" and len(env.fake.payouts) == 1
    app = env.restart()
    run(app.settle())
    s = app.db.one("SELECT * FROM settlements WHERE kind='payout'")
    assert s["state"] == "SUBMITTED" and len(env.fake.payouts) == 1
    assert s["paypal_id"] == next(iter(env.fake.payouts))  # the original batch, recovered from PayPal's error link
    assert app.case_money(cid)["paid_out"] == "40.00"


def test_payout_server_error_is_retried_with_the_same_batch_id(tmp_path):
    env = Env(tmp_path)
    _cid, fid, mid, code = happy_path(env)
    env.fake.fault("payout", "500_before")
    run(env.app.claim(fid, mid, code))
    assert not env.fake.payouts
    run(env.restart().settle())
    assert len(env.fake.payouts) == 1 and env.fake.paid_to("conductor@x.test") == 4000


def test_lost_refund_response_is_resent_safely(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    env.fake.fault("refund", "lose_response")
    run(env.app.cancel(cid))
    assert env.app.db.one("SELECT state FROM settlements WHERE kind='refund'")["state"] == "UNCERTAIN"
    run(env.restart().settle())
    assert len(env.fake.refunds) == 1 and env.fake.refunded_total() == 4000
    assert env.app.case_money(cid)["refunded"] == "40.00"


def test_unclaimed_payout_is_not_reported_as_received(tmp_path):
    env = Env(tmp_path)
    _cid, fid, mid, code = happy_path(env)
    env.fake.item_status["conductor@x.test"] = "UNCLAIMED"
    run(env.app.claim(fid, mid, code))
    run(env.app.refresh_payouts())
    s = env.app.db.one("SELECT * FROM settlements WHERE kind='payout'")
    assert s["state"] == "ITEMS_PENDING" and s["item_status"] == "UNCLAIMED"
    env.fake.item_status.clear()
    run(env.app.refresh_payouts())
    assert env.app.db.one("SELECT state FROM settlements WHERE kind='payout'")["state"] == "DONE"


def test_unpaid_case_is_never_matched(tmp_path):
    env = Env(tmp_path)
    env.lost(pay=False)
    _, _, matches = env.found()
    assert matches == []


def test_weak_matches_are_not_proposed(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    env.brain.scores[cid] = 30
    _, _, matches = env.found()
    assert matches == []


def test_different_item_types_are_not_sent_to_the_model(tmp_path):
    env = Env(tmp_path)
    env.lost(title="Black umbrella")
    _, _, matches = env.found(desc="Brown wallet on the seat")
    assert matches == []


def test_reused_photo_sends_the_payout_to_review(tmp_path):
    env = Env(tmp_path)
    _c1, _ = env.lost()
    _f1, _, _m1 = env.found()
    c2, _ = env.lost(title="Another brown wallet")
    f2, _, m2 = env.found(email="scammer@x.test")  # same photo bytes as the first report
    m = next(x for x in m2 if x["lost_id"] == c2)["match_id"]
    out = env.verify(c2, f2, m)
    assert out["status"] == "REVIEW" and any("same photo" in f for f in out["flags"])
    with pytest.raises(RuntimeError):
        env.app.handover_code(c2, m)


def test_declined_card_leaves_the_case_unpaid(tmp_path):
    env = Env(tmp_path)
    env.fake.fault("card", "decline")
    cid, _ = env.lost(pay=False)
    with pytest.raises(PayPalDefinite):
        run(env.app.pay_with_card(cid))
    assert env.app._case(cid)["status"] == "UNPAID"


def test_wallet_capture_with_lost_response_is_recovered(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost(pay=False)
    run(env.app.start_wallet_payment(cid, "https://x/r", "https://x/c"))
    env.fake.fault("capture", "lose_response")
    out = run(env.app.finish_wallet_payment(cid))
    assert out["amount"] == "41.00" and env.app._case(cid)["status"] == "OPEN"
    assert len(env.fake.captures) == 1


def test_reward_limits_and_private_detail_required(tmp_path):
    env = Env(tmp_path)
    for reward in ("1.00", "999.00"):
        with pytest.raises(ValueError):
            env.lost(reward=reward, pay=False)
    with pytest.raises(ValueError):
        env.lost(private="", pay=False)


def test_question_leak_guard():
    assert leaks("Is your daughter Asha's photo inside?", "photo of my daughter Asha", "brown wallet")
    assert not leaks("Whose photo is inside?", "photo of my daughter Asha", "brown wallet with a photo")


def test_owner_sees_the_find_only_after_the_finder_proved_it(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    fid, _, matches = env.found()
    mid = matches[0]["match_id"]
    view = env.app.owner_view(cid)["matches"][0]
    assert view["photo_id"] is None and view["finder_name"] is None
    run(env.app.finder_challenge(mid, fid))
    run(env.app.finder_answer(mid, fid, "Asha"))
    view = env.app.owner_view(cid)["matches"][0]
    assert view["photo_id"] and view["finder_name"] == "Ravi" and view["owner_question"]


def test_pending_owner_payment_does_not_open_the_case(tmp_path):
    env = Env(tmp_path)
    env.fake.capture_status = "PENDING"
    cid, _ = env.lost(pay=False)
    out = run(env.app.pay_with_card(cid))
    assert out["pending"] and env.app._case(cid)["status"] == "PAYMENT_PENDING"
    _, _, matches = env.found()
    assert matches == []  # a reward that is not completed is not on offer
    for c in env.fake.captures.values():
        c["status"] = "COMPLETED"
    run(env.app.check_pending_payments())
    assert env.app._case(cid)["status"] == "OPEN" and env.app.case_money(cid)["paid"] == "41.00"


def test_declined_owner_payment_closes_the_case(tmp_path):
    env = Env(tmp_path)
    env.fake.capture_status = "PENDING"
    cid, _ = env.lost(pay=False)
    run(env.app.pay_with_card(cid))
    for c in env.fake.captures.values():
        c["status"] = "DECLINED"
    run(env.app.check_pending_payments())
    assert env.app._case(cid)["status"] == "PAYMENT_FAILED"


def test_pending_refund_is_not_counted_until_completed(tmp_path):
    env = Env(tmp_path)
    cid, _ = env.lost()
    env.fake.refund_status = "PENDING"
    m = run(env.app.cancel(cid))
    assert m["refunded"] == "0.00" and m["settlements"][0]["state"] == "PENDING"
    for r in env.fake.refunds.values():
        r["status"] = "COMPLETED"
    run(env.app.refresh_refunds())
    assert env.app.case_money(cid)["refunded"] == "40.00"


def test_the_daily_ai_limit_survives_a_restart(tmp_path):
    from finders.ai import CappedBrain, RuleBrain

    path = str(tmp_path / "count.db")
    brain = CappedBrain(RuleBrain(), RuleBrain(), 2, counter_db=path)
    assert brain._ok() and brain._ok() and not brain._ok()
    again = CappedBrain(RuleBrain(), RuleBrain(), 2, counter_db=path)  # a new process, same database file
    assert not again._ok(peek=True) and not again._ok()
