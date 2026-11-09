# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Seed SAMPLE lost reports (labelled "sample" everywhere) so the matcher has real choices. Each sample reward is paid
with the sandbox test card, so the board shows live, funded cases. usage: python scripts/seed_demo.py <db path>"""

import asyncio
import sys
import time
from pathlib import Path

from finders.ai import GeminiBrain, Photo, RuleBrain
from finders.db import Database
from finders.paypal import PayPal
from finders.service import Finders
from finders.settings import gemini_key, load_env

PHOTOS = Path(__file__).resolve().parent.parent / "demo" / "photos"
SAMPLES = [
    # (title, public description, private detail, place, hours ago, reward, owner photo)
    ("Black Toyota car key", "Remote key, worn rubber buttons, on a plain silver ring", "the lock button is cracked",
     "MG Road metro", 5, "15.00", None),
    ("Navy blue school backpack", "Kids' backpack with a patch on the front pocket, maths books inside",
     "the name 'Meera' is written inside the flap", "Cubbon Park", 20, "25.00", None),
    ("Silver laptop in a grey sleeve", "13-inch laptop, grey felt sleeve", "a 'Kannada Rajyotsava' sticker on the lid",
     "Koramangala cafe", 30, "60.00", None),
    ("Black umbrella", "Folding umbrella, black, plain", "the strap button is missing", "Silk Board bus stop", 8,
     "5.00", None),
    ("Teddy bear", "Small light-brown teddy bear", "one ear has been stitched back with blue thread",
     "Kempegowda airport, Terminal 1", 26, "20.00", None),
    ("White earbuds case", "Wireless earbuds in a white charging case", "a yellow star sticker on the lid",
     "Indiranagar 100 Feet Road cafe", 3, "30.00", None),
]


async def main(db_path: str) -> None:
    env = load_env()
    key = gemini_key(env)
    app = Finders(
        Database(db_path), PayPal(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"]),
        GeminiBrain(key) if key else RuleBrain(),
    )  # fmt: skip
    now = time.time()
    for title, desc, private, place, hours, reward, photo in SAMPLES:
        pics = [Photo("image/jpeg", (PHOTOS / photo).read_bytes())] if photo else []
        cid, _ = await app.report_lost(
            owner_name="Sample owner", owner_email="sample.owner@example.com", title=title, description=desc,
            private_details=private, place=place, lost_at=now - hours * 3600, reward=reward, photos=pics, sample=True,
        )  # fmt: skip
        await app.pay_with_card(cid)
        print("sample case", title, reward)
    await app.paypal.aclose()


asyncio.run(main(sys.argv[1]))
