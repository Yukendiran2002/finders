# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""What the AI does in Finders. It reads, compares, asks and judges; it never moves money.

- describe: turn a photo and a few words into structured attributes (category, brand, colours, marks, contents).
- rank: compare one found item with the open lost reports and score each candidate, with reasons.
- finder_question / owner_question: write a question that only the person really holding the item (finder) or the
  person who really lost it (owner) can answer. The expected answer stays on the server.
- judge: decide whether an answer matches, with a reason. Only a clear "yes" passes; "unsure" goes to a person.

``GeminiBrain`` uses Gemini through the Nexura Agents SDK with typed outputs. ``RuleBrain`` is a deterministic stand-in
for tests and for running without a key (labelled as such in the UI).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class Attributes(BaseModel):
    category: str = Field(description="one or two words, e.g. wallet, keys, earbuds, phone, bag, umbrella, id card")
    brand: str = Field(default="", description="brand if visible or stated, else empty")
    colours: list[str] = Field(default_factory=list)
    marks: list[str] = Field(default_factory=list, description="distinctive marks: scratches, stickers, engravings")
    contents: list[str] = Field(default_factory=list, description="things inside or attached, if visible or stated")
    summary: str = Field(description="one plain sentence describing the item")


class Candidate(BaseModel):
    lost_id: str
    score: int = Field(ge=0, le=100, description="how likely this found item is that lost item")
    reasons: list[str] = Field(default_factory=list, description="short reasons for and against")


class Ranking(BaseModel):
    candidates: list[Candidate]


class Question(BaseModel):
    question: str = Field(description="one short question")
    expected: str = Field(description="the answer that proves it, in a few words")


class Verdict(BaseModel):
    verdict: Literal["yes", "no", "unsure"]
    reason: str


@dataclass
class Photo:
    mime: str
    data: bytes


@dataclass
class ItemView:
    """What the AI may see about one report. ``private`` is set only for the question writer and the judge."""

    id: str
    text: str
    place: str
    when: str
    attributes: dict[str, Any] = field(default_factory=dict)
    photos: list[Photo] = field(default_factory=list)
    private: str = ""


class Brain(Protocol):
    name: str

    async def describe(self, text: str, photos: list[Photo]) -> Attributes: ...
    async def rank(self, found: ItemView, lost: list[ItemView]) -> Ranking: ...
    async def finder_question(self, lost: ItemView) -> Question: ...
    async def owner_question(self, found: ItemView, lost: ItemView, avoid: str = "") -> Question: ...
    async def judge(self, question: str, expected: str, answer: str) -> Verdict: ...


def leaks(question: str, secret: str, public: str) -> bool:
    """True when the question repeats a distinctive word from the secret that the public text does not contain."""
    words = lambda t: {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) >= 4}
    return bool((words(secret) - words(public)) & words(question))


# --- Gemini ---------------------------------------------------------------------------------------

DESCRIBE = """You describe a personal item from a lost-and-found report. Use only what the photo shows and the text says.
Do not guess brands or contents you cannot see or that are not stated. Keep every field short."""

RANK = """You match a FOUND item against LOST reports in a lost-and-found service. For every lost report give a score
0-100 for how likely it is the same physical item, and two or three short reasons (agreements and conflicts).
Consider: item type, brand, colours, distinctive marks, contents, where and when it was lost versus found.
A different item type or a clear conflict (wrong colour, wrong brand) means a low score. Unknown details are neutral.
Score only from evidence; do not invent details. Return every lost report id you were given."""

FINDER_Q = """You protect a lost-and-found reward from people who do not have the item. Write ONE short question that
only someone physically holding this item could answer, based on the owner's PRIVATE details. The question must not
reveal the answer or any word of the private details that is not already in the public description. Prefer concrete
checkable things (a name on a card inside, the number of keys, a sticker's picture). Also give the expected answer."""

OWNER_Q = """You protect a found item from people who claim it falsely. Write ONE short question for the person who
says they lost it, about a detail that is visible in the FINDER's photo or description but is NOT mentioned in the
owner's public lost report. Only the real owner would know it. Ask about something an owner remembers without looking:
a colour, how many there are, a brand or logo, a sticker or charm, a name or initials, a case or cover. Never ask about
pose, angle, lighting, condition or anything that depends on how the photo was taken. Do not reveal the answer. Also
give the expected answer."""

JUDGE = """You check an answer in a lost-and-found verification. Say "yes" only if the answer clearly matches the
expected answer (allow synonyms, spelling slips, partial but specific matches). Say "no" if it contradicts it or is
generic. Say "unsure" if it is ambiguous. Give a one-sentence reason that does not reveal the expected answer."""


class GeminiBrain:
    name = "Gemini"

    def __init__(self, api_key: str, model: str = "gemini-3.8-flash", timeout: float = 60.0) -> None:
        self.api_key, self.model, self.timeout = api_key, model, timeout

    async def _ask(self, instructions: str, out: type[BaseModel], parts: list[Any]) -> Any:
        from nexura_agents import Agent, Runner
        from nexura_agents.providers.gemini import GeminiNativeProvider

        agent = Agent(
            name="finders-" + out.__name__.lower(),
            instructions=instructions,
            model=GeminiNativeProvider(self.model, api_key=self.api_key, timeout=self.timeout),
            output_type=out,
            max_turns=2,
        )
        res = await Runner().run_async(agent, parts, timeout=self.timeout + 20)
        if res.status != "completed":
            raise RuntimeError(f"model run {res.status}")
        value = res.final_output
        if isinstance(value, out):
            return value
        if isinstance(value, str):
            return out.model_validate_json(value)
        return out.model_validate(value)

    @staticmethod
    def _photos(photos: list[Photo]) -> list[Any]:
        from nexura_agents import Attachment

        return [Attachment.from_bytes(p.data, p.mime) for p in photos[:3]]

    async def describe(self, text: str, photos: list[Photo]) -> Attributes:
        return await self._ask(DESCRIBE, Attributes, [f"Report text: {text}", *self._photos(photos)])

    async def rank(self, found: ItemView, lost: list[ItemView]) -> Ranking:
        listing = [
            {"id": x.id, "text": x.text, "place": x.place, "lost": x.when, "attributes": x.attributes} for x in lost
        ]
        prompt = [
            "FOUND item: "
            + json.dumps(
                {"text": found.text, "place": found.place, "found": found.when, "attributes": found.attributes}
            ),
            *self._photos(found.photos),
            "LOST reports: " + json.dumps(listing),
        ]
        r: Ranking = await self._ask(RANK, Ranking, prompt)
        known = {x.id for x in lost}
        r.candidates = [c for c in r.candidates if c.lost_id in known]
        return r

    async def finder_question(self, lost: ItemView) -> Question:
        prompt = [f"PUBLIC description: {lost.text}\nPRIVATE details (never reveal): {lost.private}"]
        q: Question = await self._ask(FINDER_Q, Question, prompt)
        if leaks(q.question, lost.private, lost.text):
            q = await self._ask(
                FINDER_Q + " Your last question leaked the answer; write a different one.", Question, prompt
            )
            if leaks(q.question, lost.private, lost.text):
                raise RuntimeError("could not write a question that keeps the private details private")
        return q

    async def owner_question(self, found: ItemView, lost: ItemView, avoid: str = "") -> Question:
        prompt = [
            (
                f"Owner's PUBLIC lost report: {lost.text}\nFINDER's description: {found.text}\n"
                f"FINDER's attributes: {json.dumps(found.attributes)}\n"
                f"The finder was already asked (ask about something DIFFERENT): {avoid or 'nothing'}"
            ),
            *self._photos(found.photos),
        ]
        return await self._ask(OWNER_Q, Question, prompt)

    async def judge(self, question: str, expected: str, answer: str) -> Verdict:
        prompt = [f"Question: {question}\nExpected answer: {expected}\nGiven answer: {answer[:300]}"]
        return await self._ask(JUDGE, Verdict, prompt)


# --- deterministic stand-in -----------------------------------------------------------------------

_COLOURS = ("black", "brown", "blue", "red", "green", "white", "grey", "gray", "silver", "gold", "pink", "yellow")
_CATEGORIES = (
    "wallet", "purse", "keys", "earbuds", "airpods", "headphones", "phone", "bag", "backpack", "umbrella",
    "watch", "glasses", "card", "laptop", "bottle", "jacket", "ring", "toy", "passport",
)  # fmt: skip


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", t.lower()))


class RuleBrain:
    """Keyword rules. For tests and keyless local runs only; far weaker than the model."""

    name = "rules (no model)"

    async def describe(self, text: str, photos: list[Photo]) -> Attributes:
        w = _words(text)
        cat = next((c for c in _CATEGORIES if c in w), "item")
        return Attributes(category=cat, colours=[c for c in _COLOURS if c in w], summary=text[:120])

    async def rank(self, found: ItemView, lost: list[ItemView]) -> Ranking:
        fw = _words(found.text)
        out = []
        for x in lost:
            lw = _words(x.text)
            same_cat = found.attributes.get("category") == x.attributes.get("category")
            overlap = len(fw & lw - {"the", "and", "with", "a", "my", "of"})
            score = min(100, (50 if same_cat else 0) + overlap * 8)
            out.append(
                Candidate(lost_id=x.id, score=score, reasons=[f"{overlap} shared words", f"same type: {same_cat}"])
            )
        return Ranking(candidates=out)

    async def finder_question(self, lost: ItemView) -> Question:
        return Question(question="Describe one thing inside or attached to the item that the report does not mention.",
                        expected=lost.private)  # fmt: skip

    async def owner_question(self, found: ItemView, lost: ItemView, avoid: str = "") -> Question:
        extra = sorted(_words(found.text) - _words(lost.text))
        return Question(question="Describe a detail of your item that you did not put in your report.",
                        expected=" ".join(extra) or found.text)  # fmt: skip

    async def judge(self, question: str, expected: str, answer: str) -> Verdict:
        e, a = _words(expected), _words(answer)
        hit = len({w for w in e if len(w) >= 4} & a)
        if hit >= 1:
            return Verdict(verdict="yes", reason="The answer names a specific detail that matches.")
        return Verdict(verdict="no" if a else "unsure", reason="The answer does not match the specific detail.")
