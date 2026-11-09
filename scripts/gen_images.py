# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Generate the demo item photos with Gemini image generation (AI-generated, labelled as such in the README).
usage: GEMINI_API_KEY_FILE=... python scripts/gen_images.py [names...]"""

import base64
import json
import os
import sys
from pathlib import Path

import httpx

MODEL = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image")
OUT = Path(__file__).resolve().parent.parent / "demo" / "photos"
STYLE = (
    "Realistic smartphone photo, natural light, slightly imperfect framing, no text, no watermark, no people's faces, "
    "no brand logos."
)
SHOTS = {
    "wallet_found": "A worn brown leather bifold wallet lying on a blue plastic seat inside a city bus in Bengaluru, "
    "a small red thread tied around one corner, seen from above at an angle.",
    "keys_found": "Two brass house keys and one small silver key on a ring with a little orange fox keychain, lying on a "
    "steel metro train seat.",
    "earbuds_found": "A white wireless earbuds charging case with a small yellow star sticker on the lid, on a cafe table "
    "next to a coffee cup.",
    "backpack_found": "A navy blue school backpack with a green dinosaur patch, left on a park bench.",
    "umbrella_found": "A folded black umbrella with a wooden hooked handle leaning against a bus stop pole after rain.",
    "teddy_found": "A small light-brown teddy bear with a red bow, slightly dusty, sitting on an airport waiting-area chair.",
}
EDITS = {  # owner's "old photo" of the same item, made from the found photo so it is recognisably the same object
    "wallet_owner": ("wallet_found", "The same wallet photographed at home on a wooden desk, closed, front side up, "
                     "daylight from a window."),
}


def generate(client: httpx.Client, key: str, prompt: str, image: bytes | None = None) -> bytes:
    parts: list[dict] = [{"text": f"{prompt} {STYLE}"}]
    if image:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
    r = client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": key},
        json={"contents": [{"parts": parts}], "generationConfig": {"responseModalities": ["IMAGE"]}},
        timeout=180,
    )
    r.raise_for_status()
    for cand in r.json().get("candidates", []):
        for p in cand.get("content", {}).get("parts", []):
            data = p.get("inlineData") or p.get("inline_data")
            if data:
                return base64.b64decode(data["data"])
    raise RuntimeError("no image in response: " + json.dumps(r.json())[:300])


def main() -> None:
    key = os.environ.get("GEMINI_API_KEY") or Path(os.environ["GEMINI_API_KEY_FILE"]).expanduser().read_text().strip()
    want = set(sys.argv[1:]) or set(SHOTS) | set(EDITS)
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client:
        for name, prompt in SHOTS.items():
            if name in want:
                (OUT / f"{name}.jpg").write_bytes(generate(client, key, prompt))
                print("made", name)
        for name, (src, prompt) in EDITS.items():
            if name in want:
                (OUT / f"{name}.jpg").write_bytes(generate(client, key, prompt, (OUT / f"{src}.jpg").read_bytes()))
                print("made", name)


if __name__ == "__main__":
    main()
