# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""LOCAL FAKE PayPal for tests (httpx MockTransport). Not PayPal.

Models the sandbox behaviour Finders relies on: CAPTURE orders (a card captures at once; a wallet order captures on
POST .../capture), refunds limited to the captured amount, a repeated PayPal-Request-Id returning the original result,
a repeated Payouts sender_batch_id refused with 400 "Batch with given sender_batch_id already exists", and per-item
payout status (SUCCESS by default, settable per receiver: UNCLAIMED, PENDING, FAILED...).
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx

_ids = itertools.count(1)


def _id(p: str) -> str:
    return f"{p}{next(_ids):010d}"


def _c(v: str) -> int:
    a, b = v.split(".")
    return int(a) * 100 + int(b)


def _v(c: int) -> str:
    return f"{c // 100}.{c % 100:02d}"


class FakePayPal:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.captures: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.payouts: dict[str, dict[str, Any]] = {}
        self.idem: dict[str, tuple[int, dict[str, Any]]] = {}
        self.faults: dict[str, list[str]] = {}
        self.item_status: dict[str, str] = {}
        self.capture_status = "COMPLETED"  # status of the next order capture
        self.refund_status = "COMPLETED"  # status of the next refund
        self.transport = httpx.MockTransport(self._handle)

    def fault(self, kind: str, *faults: str) -> None:
        self.faults.setdefault(kind, []).extend(faults)

    def _take(self, kind: str) -> str | None:
        q = self.faults.get(kind) or []
        return q.pop(0) if q else None

    async def _handle(self, r: httpx.Request) -> httpx.Response:
        path, m = r.url.path, r.method
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 32400})
        body = json.loads(r.content) if r.content else {}
        rid = r.headers.get("PayPal-Request-Id")
        p = path.strip("/").split("/")
        if m == "POST" and path == "/v2/checkout/orders":
            return self._create_order(body, rid)
        if m == "POST" and p[:3] == ["v2", "checkout", "orders"] and p[-1] == "capture":
            return self._capture_order(p[3], rid)
        if m == "GET" and p[:3] == ["v2", "checkout", "orders"]:
            return httpx.Response(200, json=self._order_view(p[3]))
        if m == "GET" and p[:3] == ["v2", "payments", "captures"] and len(p) == 4:
            return httpx.Response(200, json=self.captures[p[3]])
        if m == "GET" and p[:3] == ["v2", "payments", "refunds"] and len(p) == 4:
            r = self.refunds[p[3]]
            return httpx.Response(200, json={k: v for k, v in r.items() if not k.startswith("_")})
        if p[:3] == ["v2", "payments", "captures"] and p[-1] == "refund":
            return self._refund(p[3], body, rid)
        if m == "POST" and path == "/v1/payments/payouts":
            return self._payout(body)
        if m == "GET" and p[:3] == ["v1", "payments", "payouts"]:
            b = self.payouts[p[3]]
            return httpx.Response(
                200,
                json={
                    "batch_header": {"payout_batch_id": p[3], "batch_status": "SUCCESS"},
                    "items": [
                        {"transaction_status": self.item_status.get(i["receiver"], "SUCCESS"), "payout_item": i}
                        for i in b["items"]
                    ],
                },
            )
        return httpx.Response(404, json={"name": "NOT_FOUND"})

    def _create_order(self, body: dict[str, Any], rid: str | None) -> httpx.Response:
        if rid and rid in self.idem:
            return httpx.Response(200, json=self.idem[rid][1])
        unit = body["purchase_units"][0]
        oid = _id("ORD")
        self.orders[oid] = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "unit": unit, "captures": []}
        if "card" in body.get("payment_source", {}):
            if self._take("card") == "decline":
                return httpx.Response(
                    422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]}
                )
            self._do_capture(oid)
        view = self._order_view(oid)
        if rid:
            self.idem[rid] = (201, view)
        return httpx.Response(201, json=view)

    def _do_capture(self, oid: str) -> None:
        o = self.orders[oid]
        cap = {
            "id": _id("CAP"),
            "status": self.capture_status,
            "amount": {"currency_code": "USD", "value": o["unit"]["amount"]["value"]},
            "custom_id": o["unit"].get("custom_id"),
        }
        self.captures[cap["id"]] = cap
        o["captures"].append(cap["id"])
        o["status"] = "COMPLETED"

    def _capture_order(self, oid: str, rid: str | None) -> httpx.Response:
        if rid and rid in self.idem:
            return httpx.Response(200, json=self.idem[rid][1])
        o = self.orders[oid]
        if o["status"] == "COMPLETED":
            return httpx.Response(
                422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]}
            )
        self._do_capture(oid)
        view = self._order_view(oid)
        if rid:
            self.idem[rid] = (201, view)
        if self._take("capture") == "lose_response":
            raise httpx.ReadTimeout("fake: capture processed, response lost")
        return httpx.Response(201, json=view)

    def _order_view(self, oid: str) -> dict[str, Any]:
        o = self.orders[oid]
        return {
            "id": oid,
            "status": o["status"],
            "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}],
            "purchase_units": [
                {
                    "reference_id": o["unit"].get("reference_id"),
                    "payments": {
                        "captures": [self.captures[c] for c in o["captures"]],
                        "refunds": [
                            {k: v for k, v in r.items() if not k.startswith("_")}
                            for r in self.refunds.values()
                            if r["_cap"] in o["captures"]
                        ],
                    },
                }
            ],
        }

    def _refund(self, cid: str, body: dict[str, Any], rid: str | None) -> httpx.Response:
        if rid and rid in self.idem:
            return httpx.Response(200, json=self.idem[rid][1])
        f = self._take("refund")
        if f == "timeout_before":
            raise httpx.ReadTimeout("fake: refund timed out before processing")
        cap = self.captures[cid]
        done = sum(_c(r["amount"]["value"]) for r in self.refunds.values() if r["_cap"] == cid)
        cents = _c(body["amount"]["value"])
        if done + cents > _c(cap["amount"]["value"]):
            return httpx.Response(
                422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "REFUND_AMOUNT_EXCEEDED"}]}
            )
        ref = {
            "id": _id("REF"),
            "status": self.refund_status,
            "amount": {"currency_code": "USD", "value": _v(cents)},
            "custom_id": body.get("custom_id"),
            "_cap": cid,
        }
        self.refunds[ref["id"]] = ref
        public = {k: v for k, v in ref.items() if not k.startswith("_")}
        if rid:
            self.idem[rid] = (201, public)
        if f == "lose_response":
            raise httpx.ReadTimeout("fake: refund processed, response lost")
        return httpx.Response(201, json=public)

    def _payout(self, body: dict[str, Any]) -> httpx.Response:
        sbid = body["sender_batch_header"]["sender_batch_id"]
        if any(b["sender_batch_id"] == sbid for b in self.payouts.values()):
            return httpx.Response(
                400,
                json={
                    "name": "USER_BUSINESS_ERROR",
                    "details": [
                        {
                            "issue": "Batch with given sender_batch_id already exists",
                            "link": [
                                {
                                    "href": "https://api.sandbox.paypal.com/v1/payments/payouts/"
                                    + next(k for k, b in self.payouts.items() if b["sender_batch_id"] == sbid),
                                    "rel": "self",
                                }
                            ],
                        }
                    ],
                },
            )
        f = self._take("payout")
        if f == "500_before":
            return httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"})
        pid = _id("PB")
        self.payouts[pid] = {"sender_batch_id": sbid, "items": body["items"]}
        if f == "lose_response":
            raise httpx.ReadTimeout("fake: payout accepted, response lost")
        return httpx.Response(201, json={"batch_header": {"payout_batch_id": pid, "batch_status": "PENDING"}})

    def paid_to(self, email: str) -> int:
        return sum(
            _c(i["amount"]["value"]) for b in self.payouts.values() for i in b["items"] if i["receiver"] == email
        )

    def refunded_total(self) -> int:
        return sum(_c(r["amount"]["value"]) for r in self.refunds.values())
