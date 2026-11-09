# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""HTTP API tests: the full owner + finder journey over HTTP, tokens and operator key (LOCAL FAKE PayPal)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from test_finders import Env

from finders.api import create_app

OP = {"X-Operator-Key": "op"}
JPEG = ("w.jpg", b"\xff\xd8 wallet", "image/jpeg")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def client(env):
    with TestClient(create_app(env.app, operator_key="op", housekeeping_every=3600)) as c:
        yield c


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def report_lost(client, reward="40.00"):
    r = client.post(
        "/api/lost",
        data={"owner_name": "Dev", "owner_email": "dev@x.test", "title": "Brown leather wallet",
              "description": "Lost on bus 500D", "private_details": "photo of my daughter Asha",
              "place": "Bus 500D", "lost_at": str(time.time() - 3600), "reward": reward},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()["token"]


def report_found(client, email="conductor@x.test"):
    r = client.post(
        "/api/found",
        data={"finder_name": "Ravi", "finder_email": email, "description": "Brown wallet on a seat", "place": "Depot"},
        files=[("photos", JPEG)],
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_whole_journey_over_http(client, env):
    otok = report_lost(client)
    assert client.post("/api/owner/pay/card", headers=bearer(otok)).json()["amount"] == "41.00"
    board = client.get("/api/board").json()
    assert board and board[0]["reward"] == "40.00" and "private_details" not in board[0]
    f = report_found(client)
    ftok, mid = f["token"], f["matches"][0]["match_id"]
    q = client.post("/api/finder/challenge", json={"match_id": mid}, headers=bearer(ftok)).json()
    assert q["question"]
    assert (
        client.post("/api/finder/answer", json={"match_id": mid, "answer": "Asha"}, headers=bearer(ftok)).json()[
            "verdict"
        ]
        == "yes"
    )
    ov = client.get("/api/owner", headers=bearer(otok)).json()
    assert ov["matches"][0]["owner_question"]
    r = client.post("/api/owner/answer", json={"match_id": mid, "answer": "red"}, headers=bearer(otok)).json()
    assert r["status"] == "VERIFIED"
    code = client.post("/api/owner/handover", json={"match_id": mid}, headers=bearer(otok)).json()["code"]
    out = client.post("/api/finder/claim", json={"match_id": mid, "code": code}, headers=bearer(ftok)).json()
    assert out["paid_out"] == "40.00"
    assert env.fake.paid_to("conductor@x.test") == 4000
    fv = client.get("/api/finder", headers=bearer(ftok)).json()
    assert fv["matches"][0]["payout"]["amount"] == "40.00"


def test_tokens_are_scoped(client):
    otok = report_lost(client)
    client.post("/api/owner/pay/card", headers=bearer(otok))
    f = report_found(client)
    mid = f["matches"][0]["match_id"]
    assert client.get("/api/owner").status_code == 401
    assert client.get("/api/owner", headers=bearer(f["token"])).status_code == 401  # finder token is not an owner token
    assert client.post("/api/owner/handover", json={"match_id": mid}, headers=bearer(otok)).status_code == 409
    other = report_found(client, email="o@x.test")
    r = client.post("/api/finder/challenge", json={"match_id": mid}, headers=bearer(other["token"]))
    assert r.status_code == 403


def test_operator_key_required(client):
    assert client.get("/api/ops/overview").status_code == 401
    assert client.get("/api/ops/overview", headers=OP).status_code == 200


def test_bad_uploads_are_refused(client):
    r = client.post(
        "/api/found",
        data={"finder_name": "R", "finder_email": "r@x.test", "description": "x thing", "place": "here"},
        files=[("photos", ("a.gif", b"GIF89a", "image/gif"))],
    )
    assert r.status_code == 415
    r = client.post(
        "/api/lost",
        data={"owner_name": "D", "owner_email": "d@x.test", "title": "Wallet", "private_details": "abc",
              "place": "x", "lost_at": "not-a-date", "reward": "40.00"},
    )  # fmt: skip
    assert r.status_code == 422


def test_cancel_refunds_over_http(client, env):
    otok = report_lost(client)
    client.post("/api/owner/pay/card", headers=bearer(otok))
    out = client.post("/api/owner/cancel", headers=bearer(otok)).json()
    assert out["refunded"] == "40.00" and env.fake.refunded_total() == 4000
