"""HTTP API. The website's /api/chat route proxies here; visitors never call
this service directly."""

import asyncio
import json
import logging
import os
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import agent, enquiries, knowledge
from .config import get_settings
from .crm.router import router as crm_router
from .db import Conversation, ChatLead, Message, SessionLocal, init_db
from .history import transcript
from .prompt import system_prompt
from .tools import lead_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scalryx.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    system_prompt()  # fail at startup, not on the first visitor, if knowledge is broken
    s = get_settings()
    if not (s.openai_api_key or os.environ.get("OPENAI_API_KEY")):
        log.warning("OPENAI_API_KEY is not set: every reply will be the fallback message.")
    if not s.agent_api_key:
        log.warning("AGENT_API_KEY is not set: anyone who can reach this port can use the agent.")
    if not s.calendar_enabled:
        log.warning("Cal.com is not configured: the agent will share a booking link instead of booking.")
    if not s.email_enabled:
        log.warning("SMTP is not configured (set SMTP_USER and SMTP_PASSWORD): emails will only be logged.")
    yield


app = FastAPI(title="Scalryx agent", lifespan=lifespan, docs_url=None, redoc_url=None)

# The dashboard may call /api from its own origin only, with its session cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().dashboard_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
app.include_router(crm_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Readable messages for the dashboard; the /v1 routes keep FastAPI's default."""
    if not request.url.path.startswith("/api/"):
        return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)
    errors: dict[str, str] = {}
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"] if p not in ("body", "query"))
        message = str(err["msg"]).removeprefix("Value error, ")
        errors.setdefault(field or "request", message)
    first = next(iter(errors.items()))
    return JSONResponse({"detail": f"{first[0]}: {first[1]}", "errors": errors}, status_code=422)


# ------------------------------------------------------------------ guards --


def require_agent_key(x_agent_key: str | None = Header(None)) -> None:
    expected = get_settings().agent_api_key
    if expected and not secrets.compare_digest(x_agent_key or "", expected):
        raise HTTPException(401, "Bad agent key")


def require_admin(authorization: str | None = Header(None)) -> None:
    token = get_settings().admin_token
    if not token or not secrets.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "Admin token required")


class RateLimiter:
    """Sliding one-minute window per visitor IP, in memory. One process is
    plenty for a website chat; with several workers each counts alone."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = {}

    def limited(self, key: str) -> bool:
        now = time.monotonic()
        window = self.hits.setdefault(key, deque())
        while window and now - window[0] > 60:
            window.popleft()
        window.append(now)
        if len(self.hits) > 10_000:
            self.hits = {key: window}
        return len(window) > get_settings().rate_limit_per_minute


limiter = RateLimiter()
# One turn at a time per conversation, or two replies would interleave.
locks: dict[str, asyncio.Lock] = {}


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_response(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# -------------------------------------------------------------------- chat --


class ChatRequest(BaseModel):
    session_id: str | None = Field(None, max_length=64)
    message: str = Field(min_length=1, max_length=5000)
    page: str | None = Field(None, max_length=300)
    timezone: str | None = Field(None, max_length=64)
    referrer: str | None = Field(None, max_length=300)
    utm: dict[str, str] = Field(default_factory=dict)


@app.post("/v1/chat", dependencies=[Depends(require_agent_key)])
async def chat(body: ChatRequest, request: Request, x_visitor_ip: str | None = Header(None)) -> StreamingResponse:
    s = get_settings()
    text = body.message.strip()[: s.max_message_chars]
    if not text:
        raise HTTPException(422, "Empty message")
    ip = (x_visitor_ip or (request.client.host if request.client else "unknown"))[:64]

    if limiter.limited(ip):

        async def slow_down() -> AsyncIterator[str]:
            yield sse("text", {"delta": "You're sending messages quickly. Give it a minute and try again."})
            yield sse("done", {})

        return sse_response(slow_down())

    lock = locks.setdefault(body.session_id, asyncio.Lock()) if body.session_id else asyncio.Lock()
    if lock.locked():
        raise HTTPException(409, "Still answering the previous message")

    utm = {k[:20]: v[:100] for k, v in body.utm.items() if k.startswith("utm_")}
    visit = agent.Visit(page=body.page, timezone=body.timezone, referrer=body.referrer, utm=dict(list(utm.items())[:5]))

    async def events() -> AsyncIterator[str]:
        try:
            async with lock:
                async for name, data in agent.turn(body.session_id, text, visit, ip):
                    yield sse(name, data)
            yield sse("done", {})
        finally:
            if body.session_id and not lock.locked():
                locks.pop(body.session_id, None)

    return sse_response(events())


@app.post("/v1/enquiries", dependencies=[Depends(require_agent_key)])
async def enquiry(body: enquiries.EnquiryRequest, request: Request, x_visitor_ip: str | None = Header(None)) -> dict[str, Any]:
    ip = (x_visitor_ip or (request.client.host if request.client else "unknown"))[:64]
    if limiter.limited(f"form:{ip}"):
        raise HTTPException(429, "Too many messages")
    return await enquiries.save(body, ip)


@app.get("/v1/chat/{session_id}", dependencies=[Depends(require_agent_key)])
async def history(session_id: str) -> dict[str, Any]:
    data = await agent.load_history(session_id[:64])
    if data is None:
        raise HTTPException(404, "No such conversation")
    return data


# ------------------------------------------------------------------- admin --


@app.get("/v1/admin/leads", dependencies=[Depends(require_admin)])
async def leads(tier: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    async with SessionLocal() as db:
        query = select(ChatLead).order_by(ChatLead.updated_at.desc()).limit(min(limit, 500))
        if tier:
            query = query.where(ChatLead.tier == tier)
        return [
            {
                "conversation_id": lead.conversation_id,
                "score": lead.score,
                "tier": lead.tier,
                "booked": lead.booked,
                "profile": lead.profile,
                "brief_sent": lead.brief_sent_at is not None,
                "updated_at": lead.updated_at.isoformat(),
            }
            for lead in await db.scalars(query)
        ]


@app.get("/v1/admin/conversations/{conversation_id}", dependencies=[Depends(require_admin)])
async def conversation(conversation_id: str) -> dict[str, Any]:
    async with SessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            raise HTTPException(404, "No such conversation")
        rows = await db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id))
        lead = await db.scalar(select(ChatLead).where(ChatLead.conversation_id == conv.id))
        return {
            "lead": lead_summary(conv, lead) if lead else None,
            "transcript": transcript([m.item for m in rows], limit=500),
        }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    s = get_settings()
    return {
        "ok": True,
        "model": s.openai_model,
        "calendar": s.calendar_enabled,
        "email": s.email_enabled,
        "knowledge": knowledge.load()["site"]["name"],
    }
