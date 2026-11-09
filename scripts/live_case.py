# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""LIVE SANDBOX run of one Finders case with the real Gemini model. No real money.
usage: python scripts/live_case.py <db path> <found photo path>
Needs PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET, GEMINI_API_KEY (or GEMINI_API_KEY_FILE) and
PAYPAL_SANDBOX_BUYER_EMAIL (a sandbox Personal account that receives the finder's reward)."""

import asyncio
import sys
import time
from pathlib import Path

from finders.ai import GeminiBrain, Photo
from finders.db import Database
from finders.paypal import PayPal
from finders.service import Finders
from finders.settings import gemini_key, load_env

env = load_env()


async def main(db_path: str, photo: Path) -> None:
    pp = PayPal(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"])
    app = Finders(Database(db_path), pp, GeminiBrain(gemini_key(env) or ""))
    now = time.time()
    # two other open cases so the matcher has to choose
    for title, desc, private in (
        ("Black Toyota car key", "Remote key with a worn rubber cover", "the lock button is cracked"),
        ("Brown leather wallet", "Bifold wallet with cards", "a photo of my daughter inside"),
    ):
        cid, _ = await app.report_lost(owner_name="Sample owner", owner_email="sample@example.com", title=title,
                                       description=desc, private_details=private, place="MG Road", lost_at=now - 7200,
                                       reward="10.00", sample=True)  # fmt: skip
        await app.pay_with_card(cid)
    cid, _ = await app.report_lost(
        owner_name="Dev", owner_email="dev.owner@example.com", title="House keys",
        description="Two keys on a ring with a small figure keychain", private_details="the keychain is Spider-Man",
        place="Indiranagar metro station", lost_at=now - 5400, reward="20.00",
    )  # fmt: skip
    print("owner paid:", await app.pay_with_card(cid))
    fid, _, matches = await app.report_found(
        finder_name="Ravi (station staff)", finder_email=env["PAYPAL_SANDBOX_BUYER_EMAIL"],
        description="Keys left on a metro seat", place="Indiranagar metro", found_at=now,
        photos=[Photo("image/jpeg", photo.read_bytes())],
    )  # fmt: skip
    print("matches:", [(m["lost_id"] == cid, m["score"], m["reasons"][:1]) for m in matches])
    mid = next(m["match_id"] for m in matches if m["lost_id"] == cid)
    q = await app.finder_challenge(mid, fid)
    print("finder question:", q["question"])
    print("finder answer:", await app.finder_answer(mid, fid, "It's a little Spider-Man figure"))
    oq = app.owner_view(cid)["matches"][0]["owner_question"]
    print("owner question:", oq)
    res = await app.owner_answer(cid, mid, sys.argv[3] if len(sys.argv) > 3 else input("owner answer> "))
    print("owner answer:", res)
    if res["status"] == "REVIEW":
        print("operator review:", app.review(mid, approve=True, note="checked by the operator"))
    code = app.handover_code(cid, mid, "15.00")
    print("handover:", code["reward"])
    out = await app.claim(fid, mid, code["code"])
    print("money:", {k: out[k] for k in ("paid", "paid_out", "refunded", "held")})
    for s in out["settlements"]:
        print("  ", s["kind"], s["amount"], s["state"], s["paypal_id"])
    await asyncio.sleep(8)
    await app.refresh_payouts(cid)
    print("after refresh:", [(s["kind"], s["state"], s["item_status"]) for s in app.case_money(cid)["settlements"]])
    await pp.aclose()


asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
