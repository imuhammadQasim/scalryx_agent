"""One visitor turn: stream the model's reply, run its tools, persist everything.

Uses the OpenAI Responses API statelessly (`store=False`): the conversation
lives in our database as Responses input items and is replayed on every
request. Reasoning items come back encrypted and are replayed as received,
so the model keeps its reasoning across tool calls without OpenAI storing
the chat. History is append-only, which also keeps the prompt cache warm.
"""

import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import openai
from openai import AsyncOpenAI
from sqlalchemy import select

from . import knowledge, tools
from .config import get_settings
from .db import Booking, Conversation, ChatLead, Message, SessionLocal, get_lead, utcnow
from .history import display_messages
from .prompt import system_prompt
from .scoring import brief_card

log = logging.getLogger("scalryx.agent")

Event = tuple[str, dict[str, Any]]

RETURN_GAP = timedelta(hours=6)

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    _client = _client or AsyncOpenAI(api_key=get_settings().openai_api_key or None)
    return _client


@dataclass
class Visit:
    """What the website knows about the visitor, sent with every message."""

    page: str | None = None
    timezone: str | None = None
    referrer: str | None = None
    utm: dict[str, str] = field(default_factory=dict)


def _contact_line() -> str:
    site = knowledge.load()["site"]
    return f"email {site['email']} or WhatsApp {site['whatsapp']}"


def _fallback_text() -> str:
    return f"Sorry, I couldn't answer that just now. Please try again, or {_contact_line()} and a person will reply."


@lru_cache
def _request_params() -> dict[str, Any]:
    """Everything except `input`. Built once so the cached prefix
    (instructions and tools) stays identical across requests."""
    s = get_settings()
    params: dict[str, Any] = {
        "model": s.openai_model,
        "instructions": system_prompt(),
        "tools": tools.definitions(),
        "store": False,
        # Reasoning and the visible reply share this budget.
        "max_output_tokens": 8000,
        # Routes every conversation to the same cache for the shared prefix.
        "prompt_cache_key": "scalryx-agent",
    }
    if s.openai_reasoning_effort:
        params["reasoning"] = {"effort": s.openai_reasoning_effort}
        # With store=False, reasoning is only replayable in encrypted form.
        params["include"] = ["reasoning.encrypted_content"]
    return params


def _context_message(conv: Conversation, visit: Visit, returning: bool, profile: dict[str, Any]) -> str:
    tz = tools.visitor_tz(conv)
    now = datetime.now(tools.zone(tz) or timezone.utc)
    hour = now.hour % 12 or 12
    assumed = "" if conv.timezone else ", assumed: their own time zone is unknown"
    lines = [
        "Visitor context from the website (not typed by the visitor):",
        f"Page: {visit.page or 'unknown'}",
        f"Visitor's local time: {now:%A}, {now.day} {now:%B %Y}, {hour}:{now:%M} {'AM' if now.hour < 12 else 'PM'} ({tz}{assumed})",
    ]
    source = [f"referrer {visit.referrer}"] if visit.referrer else []
    source += [f"{k}={v}" for k, v in visit.utm.items()]
    if source:
        lines.append("Came from: " + ", ".join(source))
    if returning:
        known = {k: v for k, v in profile.items() if v not in (None, "", [])}
        lines.append(f"Returning visitor. What we already know: {known or 'nothing yet'}. Welcome them back briefly.")
    if not get_settings().calendar_enabled:
        lines.append("To book, check_availability returns a booking link: share it as the way to pick a time, without saying in-chat booking is unavailable.")
    return "\n".join(lines)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class EchoGuard:
    """Models occasionally start a reply by echoing a tool result as text,
    e.g. `{"saved": [...]}That would save...`. Text that starts with `{` is
    held back until it is clear whether it is a JSON object, which is dropped."""

    LIMIT = 4000

    def __init__(self) -> None:
        self.buffer = ""
        self.passing = False

    def feed(self, delta: str) -> str:
        if self.passing:
            return delta
        self.buffer += delta
        while True:
            head = self.buffer.lstrip()
            if not head:
                return ""
            if not head.startswith("{"):
                break
            try:
                _, end = json.JSONDecoder().raw_decode(head)
            except json.JSONDecodeError:
                if len(head) < self.LIMIT:
                    return ""  # maybe an unfinished object: wait for more
                break
            self.buffer = head[end:]
        self.passing = True
        out, self.buffer = self.buffer, ""
        return out

    def flush(self) -> str:
        if self.passing:
            return ""
        self.passing = True
        return strip_json_echo(self.buffer)


def strip_json_echo(text: str) -> str:
    """Drops JSON objects at the start of `text`; returns it unchanged otherwise."""
    rest = text
    while (head := rest.lstrip()).startswith("{"):
        try:
            _, end = json.JSONDecoder().raw_decode(head)
        except json.JSONDecodeError:
            break
        rest = head[end:].lstrip()
    return rest


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    """Store assistant text without echoed JSON, so the model never sees
    itself doing it and the restored chat stays clean."""
    if item.get("type") != "message":
        return item
    parts = []
    for part in item.get("content") or []:
        if part.get("type") == "output_text":
            clean = strip_json_echo(part.get("text", ""))
            # An all-JSON message keeps its text: the item must stay replayable.
            if clean and clean != part.get("text"):
                part = {**part, "text": clean}
        parts.append(part)
    return {**item, "content": parts}


async def turn(conversation_id: str | None, text: str, visit: Visit, ip: str | None) -> AsyncIterator[Event]:
    s = get_settings()
    async with SessionLocal() as db:
        conv = await db.get(Conversation, conversation_id) if conversation_id else None
        returning = False
        if conv is None:
            conv = Conversation(
                id=secrets.token_urlsafe(24),
                ip=ip,
                timezone=visit.timezone if tools.zone(visit.timezone) else None,
                source={"page": visit.page, "referrer": visit.referrer, **visit.utm},
                state={},
                user_turns=0,
            )
            db.add(conv)
            first = True
        else:
            first = False
            returning = utcnow() - _aware(conv.last_active_at) > RETURN_GAP
            if visit.timezone and tools.zone(visit.timezone):
                conv.timezone = visit.timezone
        yield "session", {"id": conv.id}

        if conv.user_turns >= s.max_user_turns:
            yield "text", {"delta": f"We've covered a lot! To keep going, {_contact_line()} and the team will pick it up from here."}
            return

        lead = await get_lead(db, conv.id)
        rows = await db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id))
        items = [m.item for m in rows]

        def append(item: dict[str, Any]) -> None:
            items.append(item)
            db.add(Message(conversation_id=conv.id, kind=item.get("role") or item["type"], item=item))

        # Nothing is committed until the first model step finishes, so a
        # visitor who disconnects mid-reply leaves no half-written turn.
        if first or returning:
            append({"role": "developer", "content": _context_message(conv, visit, returning, lead.profile)})
        append({"role": "user", "content": text})
        conv.user_turns += 1
        conv.last_active_at = utcnow()

        ctx = tools.ToolContext(db=db, conversation=conv, lead=lead, items=items)
        shown_error: str | None = None
        try:
            async for event in _loop(ctx, append, db):
                yield event
        except openai.APIError as exc:
            log.error("OpenAI API error %s: %s", getattr(exc, "status_code", "?"), exc)
            shown_error = _fallback_text()
        except Exception:
            log.exception("Turn failed")
            shown_error = _fallback_text()

        if shown_error:
            yield "text", {"delta": shown_error}
            # Keep the stored chat matching what the visitor saw.
            append({"role": "assistant", "content": shown_error})
        await db.commit()


async def _loop(ctx: tools.ToolContext, append: Callable[[dict[str, Any]], None], db) -> AsyncIterator[Event]:
    s = get_settings()
    params = _request_params()
    wrote = False

    for _ in range(s.max_steps_per_turn):
        gap = wrote  # separate this step's text from the previous step's
        step_wrote = False
        response = None
        guards: dict[str, EchoGuard] = {}

        def shown(text: str) -> Event | None:
            nonlocal step_wrote, wrote
            if not text:
                return None
            if gap and not step_wrote:
                text = "\n\n" + text.lstrip()
            step_wrote = wrote = True
            return "text", {"delta": text}

        stream = await client().responses.create(
            **params, input=ctx.items, stream=True, safety_identifier=ctx.conversation.id
        )
        async for event in stream:
            if event.type in ("response.output_text.delta", "response.refusal.delta"):
                guard = guards.setdefault(getattr(event, "item_id", ""), EchoGuard())
                if out := shown(guard.feed(event.delta)):
                    yield out
            elif event.type in ("response.completed", "response.incomplete"):
                response = event.response
            elif event.type == "response.failed":
                error = getattr(event.response, "error", None)
                raise RuntimeError(f"Response failed: {getattr(error, 'message', error)}")
            elif event.type == "error":
                raise RuntimeError(f"Stream error: {getattr(event, 'message', event)}")
        for guard in guards.values():
            if out := shown(guard.flush()):
                yield out
        if response is None:
            raise RuntimeError("Stream ended without a final response")

        output = [_clean(o.model_dump(mode="json", exclude_none=True)) for o in response.output]
        calls = [o for o in output if o["type"] == "function_call"]
        if response.status == "incomplete" and calls:
            # Arguments may be cut off; never run a half-written tool call.
            raise RuntimeError(f"Tool call cut off: {response.incomplete_details}")

        for item in output:
            append(item)
        if not calls:
            await db.commit()
            return

        for call in calls:
            try:
                arguments = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                content, is_error = json.dumps({"error": "Arguments were not valid JSON. Try again."}), True
            else:
                try:
                    content, is_error = await tools.run(ctx, call["name"], arguments)
                except Exception:
                    log.exception("Tool %s crashed", call["name"])
                    content, is_error = json.dumps({"error": "The tool failed. Carry on without it."}), True
            if is_error:
                log.info("Tool %s returned an error: %s", call["name"], content)
            append({"type": "function_call_output", "call_id": call["call_id"], "output": content})
            for event in ctx.events:
                yield event
            ctx.events.clear()
        # The calls and their outputs are saved together, in one commit.
        await db.commit()

    # Out of steps with tools still running: close the turn politely.
    text = f"Let me hand this to the team so nothing gets missed. You can also {_contact_line()}."
    yield "text", {"delta": ("\n\n" if wrote else "") + text}
    append({"role": "assistant", "content": text})


async def load_history(conversation_id: str) -> dict[str, Any] | None:
    async with SessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return None
        rows = await db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id))
        lead = await db.scalar(select(ChatLead).where(ChatLead.conversation_id == conv.id))
        booking = await db.scalar(
            select(Booking).where(Booking.conversation_id == conv.id).order_by(Booking.id.desc())
        )
        tz = tools.visitor_tz(conv)
        return {
            "messages": display_messages([m.item for m in rows]),
            "brief": brief_card(lead.profile) if lead and lead.profile else None,
            "booking": {
                "label": tools.slot_label(_aware(booking.start), tz),
                "timezone": tz,
                "start": _aware(booking.start).isoformat(),
                "location": booking.location,
                "email": booking.attendee_email,
            }
            if booking
            else None,
        }
