# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Check a case's money against PayPal's own records.

The owner's payment and refunds are read through PayPal's official Agent Toolkit (``paypal-agent-toolkit``,
``get_order_details``, read-only). The finder's payout is read with the Payouts API (the toolkit has no payouts tool).
Code compares the numbers with the ledger; no model decides pass or fail. A Gemini agent can then answer the owner's
questions with the same read-only tools.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from finders.db import Database
from finders.money import fmt, to_cents
from finders.paypal import PayPal

SANDBOX = "https://api-m.sandbox.paypal.com"
READ_ONLY = frozenset({"get_order_details", "list_transactions"})


class ToolkitReader:
    def __init__(self, client_id: str, client_secret: str) -> None:
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context

        self._api = PayPalAPI(client_id, client_secret, Context(sandbox=True, source="FINDERS"))
        if self._api._paypal_client.base_url != SANDBOX:
            raise RuntimeError("PayPal Agent Toolkit is not pointed at the sandbox")

    async def run(self, method: str, params: dict[str, Any]) -> Any:
        if method not in READ_ONLY:
            raise PermissionError(f"{method} is not a read-only toolkit method")
        out = await asyncio.wait_for(asyncio.to_thread(self._api.run, method, params), 30.0)
        return json.loads(out) if isinstance(out, str) else out

    async def order(self, order_id: str) -> dict[str, Any]:
        res = await self.run("get_order_details", {"order_id": order_id})
        return res.get("raw", res)


def _payments(order: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [x for u in order.get("purchase_units", []) for x in (u.get("payments", {}).get(key) or [])]


async def audit_case(db: Database, reader: Any, paypal: PayPal, case_id: str) -> dict[str, Any]:
    case = db.one("SELECT * FROM lost WHERE id=?", case_id)
    if case is None:
        raise KeyError(case_id)
    if not case["order_id"]:
        raise RuntimeError("the reward was never paid")
    order = await reader.order(case["order_id"])
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, ours: str, theirs: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "finders": ours, "paypal": theirs})

    caps = [
        c for c in _payments(order, "captures") if c.get("status") in ("COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED")
    ]
    cap = next((c for c in caps if c["id"] == case["capture_id"]), None)
    check(
        "Owner paid reward + fee",
        cap is not None and to_cents(cap["amount"]["value"]) == case["paid_cents"],
        f"{fmt(case['paid_cents'])} capture {case['capture_id']}",
        "missing" if cap is None else f"{cap['amount']['value']} {cap.get('status')}",
    )
    check("Only one payment taken", len(caps) == 1, "1", str(len(caps)))
    refunds = [r for r in _payments(order, "refunds") if r.get("status") in ("COMPLETED", "PENDING")]
    paypal_ref = sum(to_cents(r["amount"]["value"]) for r in refunds)
    check("Refunded to owner", paypal_ref == case["refunded_cents"], fmt(case["refunded_cents"]), fmt(paypal_ref))
    payouts = db.all("SELECT * FROM settlements WHERE lost_id=? AND kind='payout'", case_id)
    for s in payouts:
        if not s["paypal_id"]:
            check("Finder payout", False, f"{fmt(s['amount_cents'])} {s['state']}", "no batch id recorded")
            continue
        res = await paypal.get_payout(s["paypal_id"])
        items = res.get("items", [])
        amt = items[0].get("payout_item", {}).get("amount", {}).get("value") if items else None
        st = items[0].get("transaction_status") if items else None
        check(
            "Finder payout (Payouts API)",
            amt is not None and to_cents(amt) == s["amount_cents"],
            f"{fmt(s['amount_cents'])} batch {s['paypal_id']}",
            f"{amt} {st}",
        )
    total_out = case["paid_out_cents"] + case["refunded_cents"]
    check(
        "Never more out than the reward",
        total_out <= case["reward_cents"],
        f"out {fmt(total_out)} of reward {fmt(case['reward_cents'])}",
        f"refunds {fmt(paypal_ref)}",
    )
    result = {"ok": all(c["ok"] for c in checks), "order_id": case["order_id"], "checks": checks}
    db.event("audit_completed", case_id, ok=result["ok"], checks=checks)
    return result


ASK = """You answer questions about the money in one Finders lost-and-found case. Finders is a PayPal sandbox demo:
the owner pays a reward plus a $1.00 fee up front; the finder is paid with PayPal Payouts after the owner gives them a
handover code; unused rewards are refunded after 14 days or on cancel (the fee is kept).
Use the tools; never guess amounts or ids; say when a figure comes from PayPal's own records. You cannot move money
and must not offer to. At most 5 short sentences."""


async def ask_about_case(
    db: Database, reader: Any, paypal: PayPal, case_id: str, question: str, *, api_key: str,
    model: str = "gemini-3.8-flash",
) -> dict[str, Any]:  # fmt: skip
    from nexura_agents import Agent, Runner, tool
    from nexura_agents.providers.gemini import GeminiNativeProvider

    case = db.one("SELECT * FROM lost WHERE id=?", case_id)
    if case is None:
        raise KeyError(case_id)
    used: list[str] = []

    @tool(description="PayPal's record of the owner's payment and refunds for this case (PayPal Agent Toolkit).")
    async def paypal_order() -> str:
        used.append("paypal_order")
        if not case["order_id"]:
            return "No PayPal order yet."
        o = await reader.order(case["order_id"])
        return json.dumps(
            {
                "status": o.get("status"),
                "captures": [{k: c.get(k) for k in ("id", "status", "amount")} for c in _payments(o, "captures")],
                "refunds": [{k: r.get(k) for k in ("id", "status", "amount")} for r in _payments(o, "refunds")],
            }
        )

    @tool(description="Finders' ledger for this case: reward, fee, payouts to the finder and refunds, with states.")
    def ledger() -> str:
        used.append("ledger")
        rows = db.all(
            "SELECT kind, amount_cents, state, item_status, paypal_id FROM settlements WHERE lost_id=?", case_id
        )
        return json.dumps(
            {
                "status": case["status"], "reward": fmt(case["reward_cents"]), "fee": fmt(case["fee_cents"]),
                "settlements": [{**dict(r), "amount": fmt(r["amount_cents"])} for r in rows],
            }
        )  # fmt: skip

    agent = Agent(
        name="finders-money-questions",
        instructions=ASK,
        model=GeminiNativeProvider(model, api_key=api_key, timeout=40.0),
        tools=[paypal_order, ledger],
        max_turns=6,
    )
    res = await Runner().run_async(agent, question[:500], timeout=90)
    answer = str(res.final_output or "").strip() if res.status == "completed" else ""
    answer = answer or "I could not answer that right now."
    db.event("money_question", case_id, question=question[:500], answer=answer[:2000], tools=used)
    return {"answer": answer, "tools": used}
