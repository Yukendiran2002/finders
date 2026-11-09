# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""HTTP API for Finders (FastAPI).

Three audiences, each with its own credential:
- owners: a personal token returned when they report a lost item (Authorization: Bearer ...);
- finders: a personal token returned when they report a found item;
- the operator console: the operator key (X-Operator-Key), for review, audit and the live feed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from finders.ai import Photo
from finders.money import MoneyError
from finders.paypal import PayPalError
from finders.records import ask_about_case, audit_case
from finders.service import Finders

MAX_PHOTOS = 3
MAX_PHOTO_BYTES = 8 * 1024 * 1024


class AnswerIn(BaseModel):
    match_id: str = Field(min_length=1, max_length=64)
    answer: str = Field(min_length=1, max_length=300)


class MatchIn(BaseModel):
    match_id: str = Field(min_length=1, max_length=64)


class HandoverIn(BaseModel):
    match_id: str = Field(min_length=1, max_length=64)
    reward: str | None = Field(default=None, pattern=r"^\d{1,3}(\.\d{1,2})?$")


class ClaimIn(BaseModel):
    match_id: str = Field(min_length=1, max_length=64)
    code: str = Field(pattern=r"^\s*\d{6}\s*$")


class ReviewIn(BaseModel):
    match_id: str = Field(min_length=1, max_length=64)
    approve: bool
    note: str = Field(default="", max_length=200)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


async def _photos(files: list[UploadFile] | None) -> list[Photo]:
    out = []
    for f in (files or [])[:MAX_PHOTOS]:
        data = await f.read(MAX_PHOTO_BYTES + 1)
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "each photo must be 8 MB or less")
        mime = (f.content_type or "").lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(415, "photos must be JPEG, PNG or WebP")
        if data:
            out.append(Photo(mime, data))
    return out


def _ts(value: str) -> float:
    """Accepts epoch seconds or an ISO date-time from a form."""
    try:
        return float(value)
    except ValueError:
        from datetime import datetime

        dt = datetime.fromisoformat(value)
        return dt.timestamp()


def _ai_budget(service: Finders) -> bool:
    """Money questions share the daily model budget with matching (CappedBrain)."""
    ok = getattr(service.brain, "_ok", None)
    return ok() if ok else True


def create_app(
    service: Finders,
    *,
    operator_key: str | None,
    paypal_client_id: str | None = None,
    records: Any = None,
    gemini_key: str | None = None,
    static_dir: Path | None = None,
    housekeeping_every: float = 60.0,
) -> FastAPI:
    lock = asyncio.Lock()

    async def housekeeping() -> None:
        while True:
            await asyncio.sleep(housekeeping_every)
            try:
                async with lock:
                    await service.check_pending_payments()
                    await service.expire_due()
                    await service.settle()
                    await service.refresh_payouts()
                    await service.refresh_refunds()
            except Exception as exc:  # noqa: BLE001 - recorded; the next pass retries
                service.db.event("housekeeping_failed", None, error=f"{type(exc).__name__}: {exc}"[:300])

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.settle()  # finish anything a previous process left unsent or uncertain
        task = asyncio.create_task(housekeeping())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    api = FastAPI(title="Finders", version="0.1.0", lifespan=lifespan)

    def operator(x_operator_key: str = Header(default="")) -> None:
        if operator_key is None:
            return
        given = x_operator_key  # header only: keys never appear in URLs or access logs
        if not given or not hmac.compare_digest(given.encode(), operator_key.encode()):
            raise HTTPException(401, "operator key required")

    def owner(authorization: str = Header(default="")) -> str:
        cid = service.owner_for_token(authorization.removeprefix("Bearer ").strip())
        if cid is None:
            raise HTTPException(401, "owner token required")
        return cid

    def finder(authorization: str = Header(default="")) -> str:
        fid = service.finder_for_token(authorization.removeprefix("Bearer ").strip())
        if fid is None:
            raise HTTPException(401, "finder token required")
        return fid

    def fail(exc: Exception) -> HTTPException:
        if isinstance(exc, PermissionError):
            return HTTPException(403, str(exc))
        if isinstance(exc, KeyError):
            return HTTPException(404, "not found")
        if isinstance(exc, (ValueError, MoneyError)):
            return HTTPException(422, str(exc))
        if isinstance(exc, PayPalError):
            return HTTPException(502, f"PayPal: {exc.name} {exc.issues}")
        return HTTPException(409, str(exc))

    async def guarded(coro: Any) -> Any:
        try:
            async with lock:
                return await coro
        except (PermissionError, KeyError, ValueError, MoneyError, PayPalError, RuntimeError) as exc:
            raise fail(exc) from None

    # --- public ---------------------------------------------------------------------------------
    @api.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "paypal": "sandbox", "ai": service.brain.name}

    @api.get("/api/config")
    def config() -> dict[str, Any]:
        return {"paypal_client_id": paypal_client_id, "environment": "sandbox", "ai": service.brain.name}

    @api.get("/api/board")
    def board() -> list[dict[str, Any]]:
        """Open rewards anyone can browse (public fields only)."""
        return [
            {k: c[k] for k in ("id", "title", "description", "place", "lost_at", "reward", "sample", "photo_id")}
            for c in service.cases()
            if c["status"] == "OPEN"
        ]

    @api.get("/api/photos/{photo_id}")
    def photo(photo_id: str) -> Response:
        """Photo ids are random and only handed out where the viewer may see the photo."""
        p = service.photo(photo_id)
        if p is None:
            raise HTTPException(404, "not found")
        return Response(content=p[1], media_type=p[0], headers={"Cache-Control": "private, max-age=3600"})

    @api.post("/api/lost")
    async def report_lost(
        owner_name: str = Form(..., min_length=1, max_length=60),
        owner_email: str = Form(..., min_length=3, max_length=254),
        title: str = Form(..., min_length=2, max_length=80),
        description: str = Form("", max_length=1000),
        private_details: str = Form(..., min_length=3, max_length=500),
        place: str = Form(..., min_length=2, max_length=120),
        lost_at: str = Form(...),
        reward: str = Form(..., pattern=r"^\d{1,3}(\.\d{1,2})?$"),
        photos: list[UploadFile] | None = File(default=None),
    ) -> dict[str, Any]:
        pics = await _photos(photos)
        try:
            when = _ts(lost_at)
        except ValueError:
            raise HTTPException(422, "lost_at must be a date-time") from None
        cid, token = await guarded(
            service.report_lost(
                owner_name=owner_name, owner_email=owner_email, title=title, description=description,
                private_details=private_details, place=place, lost_at=when, reward=reward, photos=pics,
            )
        )  # fmt: skip
        return {"case_id": cid, "token": token}

    @api.post("/api/found")
    async def report_found(
        finder_name: str = Form(..., min_length=1, max_length=60),
        finder_email: str = Form(..., min_length=3, max_length=254),
        description: str = Form(..., min_length=2, max_length=1000),
        place: str = Form(..., min_length=2, max_length=120),
        found_at: str = Form(""),
        photos: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        pics = await _photos(photos)
        when = _ts(found_at) if found_at else time.time()
        fid, token, matches = await guarded(
            service.report_found(
                finder_name=finder_name, finder_email=finder_email, description=description, place=place,
                found_at=when, photos=pics,
            )
        )  # fmt: skip
        return {"found_id": fid, "token": token, "matches": matches}

    # --- owner ----------------------------------------------------------------------------------
    @api.get("/api/owner")
    def owner_view(cid: str = Depends(owner)) -> dict[str, Any]:
        return service.owner_view(cid)

    @api.post("/api/owner/pay/card")
    async def pay_card(cid: str = Depends(owner)) -> dict[str, Any]:
        return await guarded(service.pay_with_card(cid))

    @api.post("/api/owner/pay/wallet/order")
    async def pay_wallet_order(request: Request, cid: str = Depends(owner)) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return await guarded(service.start_wallet_payment(cid, f"{base}/lost?paid=1", f"{base}/lost?cancel=1"))

    @api.post("/api/owner/pay/wallet/capture")
    async def pay_wallet_capture(cid: str = Depends(owner)) -> dict[str, Any]:
        return await guarded(service.finish_wallet_payment(cid))

    @api.post("/api/owner/answer")
    async def owner_answer(body: AnswerIn, cid: str = Depends(owner)) -> dict[str, Any]:
        return await guarded(service.owner_answer(cid, body.match_id, body.answer))

    @api.post("/api/owner/handover")
    async def handover(body: HandoverIn, cid: str = Depends(owner)) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            return service.handover_code(cid, body.match_id, body.reward)

        return await guarded(go())

    @api.post("/api/owner/cancel")
    async def cancel(cid: str = Depends(owner)) -> dict[str, Any]:
        return await guarded(service.cancel(cid))

    @api.post("/api/owner/ask")
    async def owner_ask(body: AskIn, cid: str = Depends(owner)) -> dict[str, Any]:
        if records is None or not gemini_key or not _ai_budget(service):
            raise HTTPException(503, "questions need the PayPal Agent Toolkit reader and a Gemini key")
        try:
            await service.refresh_payouts(cid)
            return await ask_about_case(service.db, records, service.paypal, cid, body.question, api_key=gemini_key)
        except Exception as exc:  # noqa: BLE001 - read-only; shown to the owner
            raise HTTPException(502, f"could not answer: {type(exc).__name__}") from None

    # --- finder ---------------------------------------------------------------------------------
    @api.get("/api/finder")
    def finder_view(fid: str = Depends(finder)) -> dict[str, Any]:
        return service.finder_view(fid)

    @api.post("/api/finder/challenge")
    async def challenge(body: MatchIn, fid: str = Depends(finder)) -> dict[str, Any]:
        return await guarded(service.finder_challenge(body.match_id, fid))

    @api.post("/api/finder/answer")
    async def finder_answer(body: AnswerIn, fid: str = Depends(finder)) -> dict[str, Any]:
        return await guarded(service.finder_answer(body.match_id, fid, body.answer))

    @api.post("/api/finder/claim")
    async def claim(body: ClaimIn, fid: str = Depends(finder)) -> dict[str, Any]:
        return await guarded(service.claim(fid, body.match_id, body.code))

    # --- operator -------------------------------------------------------------------------------
    @api.get("/api/ops/overview", dependencies=[Depends(operator)])
    def overview() -> dict[str, Any]:
        return {"cases": service.cases(), "found": service.found_items(), "matches": service.all_matches()}

    @api.post("/api/ops/review", dependencies=[Depends(operator)])
    async def review(body: ReviewIn) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            return service.review(body.match_id, body.approve, body.note)

        return await guarded(go())

    @api.post("/api/ops/housekeeping", dependencies=[Depends(operator)])
    async def run_housekeeping() -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            await service.check_pending_payments()
            expired = await service.expire_due()
            await service.settle()
            await service.refresh_payouts()
            await service.refresh_refunds()
            return {"expired": expired}

        return await guarded(go())

    @api.post("/api/ops/cases/{case_id}/audit", dependencies=[Depends(operator)])
    async def audit(case_id: str) -> dict[str, Any]:
        if records is None:
            raise HTTPException(503, "PayPal Agent Toolkit reader not configured")
        try:
            await service.refresh_payouts(case_id)
            return await audit_case(service.db, records, service.paypal, case_id)
        except (KeyError, RuntimeError) as exc:
            raise fail(exc) from None
        except Exception as exc:  # noqa: BLE001 - the toolkit raises requests errors
            raise HTTPException(502, f"PayPal: {type(exc).__name__}") from None

    @api.post("/api/ops/cases/{case_id}/ask", dependencies=[Depends(operator)])
    async def ops_ask(case_id: str, body: AskIn) -> dict[str, Any]:
        if records is None or not gemini_key or not _ai_budget(service):
            raise HTTPException(503, "questions need the PayPal Agent Toolkit reader and a Gemini key")
        try:
            await service.refresh_payouts(case_id)
            return await ask_about_case(service.db, records, service.paypal, case_id, body.question, api_key=gemini_key)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not answer: {type(exc).__name__}") from None

    @api.get("/api/ops/events", dependencies=[Depends(operator)])
    async def stream(request: Request, after: int = 0) -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            last, idle = after, 0
            while not await request.is_disconnected():
                batch = service.events(after=last)
                for e in batch:
                    last = e["seq"]
                    yield f"id: {e['seq']}\nevent: {e['kind']}\ndata: {json.dumps(e)}\n\n"
                idle = 0 if batch else idle + 1
                if idle and idle % 30 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    if static_dir is not None and (static_dir / "index.html").exists():
        api.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @api.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = (static_dir / path).resolve()
            if path and candidate.is_file() and static_dir.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return api


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    from finders.ai import CappedBrain, GeminiBrain, RuleBrain
    from finders.db import Database
    from finders.paypal import PayPal
    from finders.records import ToolkitReader
    from finders.settings import gemini_key, load_env

    env = load_env()
    key = gemini_key(env)
    db_path = env.get("FINDERS_DB", "finders.db")
    brain = (
        CappedBrain(GeminiBrain(key), RuleBrain(), int(env.get("GEMINI_DAILY_LIMIT", "400")), counter_db=db_path)
        if key
        else RuleBrain()
    )
    service = Finders(
        Database(db_path),
        PayPal(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"]),
        brain,
    )
    operator_key = env.get("FINDERS_OPERATOR_KEY")
    if not operator_key and env.get("FINDERS_OPEN_CONSOLE") != "1":
        raise SystemExit("set FINDERS_OPERATOR_KEY (or FINDERS_OPEN_CONSOLE=1 for local development)")
    app = create_app(
        service,
        operator_key=operator_key or None,
        paypal_client_id=env["PAYPAL_CLIENT_ID"],
        records=ToolkitReader(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"]),
        gemini_key=key,
        static_dir=Path(env.get("FINDERS_WEB", "web/dist")),
    )
    uvicorn.run(app, host=env.get("HOST", "127.0.0.1"), port=int(env.get("PORT", "8078")))
