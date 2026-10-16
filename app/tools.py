"""The agent's tools: definitions for the model, input models, and handlers.

Handlers own every side effect and every number the visitor sees (scores,
ROI, slots, prices), so the model can't invent them.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from . import knowledge
from .crm import service as crm
from .config import get_settings
from .db import Booking, Conversation, ChatLead, utcnow
from .history import transcript
from .integrations import calcom, mailer
from .scoring import BUDGETS, TIMELINES, assess, brief_card

# ----------------------------------------------------------------- context --


@dataclass
class ToolContext:
    db: AsyncSession
    conversation: Conversation
    lead: ChatLead
    items: list[dict[str, Any]]
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))

    @property
    def tz(self) -> str:
        return visitor_tz(self.conversation)


class ToolFailure(Exception):
    """An expected failure, returned to the model as an error result."""


# ----------------------------------------------------------------- helpers --


def visitor_tz(conversation: Conversation) -> str:
    """The visitor's own time zone, or the US default when it is unknown."""
    return conversation.timezone or get_settings().default_timezone


def zone(name: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def slot_label(dt: datetime, tz: str) -> str:
    local = dt.astimezone(zone(tz) or timezone.utc)
    hour = local.hour % 12 or 12
    return f"{local:%a}, {local:%b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def _price(tier: dict[str, Any]) -> float | None:
    digits = re.sub(r"[^\d.]", "", tier["price"]["amount"])
    return float(digits) if digits else None


def _fallback_links() -> dict[str, str]:
    s, site = get_settings(), knowledge.load()["site"]
    return {
        "booking_link": s.cal_booking_url or f"{site['url']}/contact",
        "email": site["email"],
        "whatsapp": site["whatsapp"],
    }


def _rescore(ctx: ToolContext) -> Any:
    a = assess(ctx.lead.profile, ctx.lead.booked)
    ctx.lead.score, ctx.lead.tier = a.score, a.tier
    ctx.emit("brief", brief_card(ctx.lead.profile))
    return a


def lead_summary(conversation: Conversation, lead: ChatLead) -> str:
    p = lead.profile
    budget = BUDGETS.get(p.get("budget") or "", ("unknown",))[0]
    timeline = TIMELINES.get(p.get("timeline") or "", ("unknown",))[0]
    rows = [
        ("Name", p.get("name")),
        ("Email", p.get("email")),
        ("Phone / WhatsApp", p.get("phone")),
        ("Company", p.get("company")),
        ("Website", p.get("website")),
        ("Industry", p.get("industry")),
        ("Location", p.get("location")),
        ("Role", p.get("role")),
        ("Decision maker", {True: "yes", False: "no"}.get(p.get("decision_maker"))),
        ("Interested in", ", ".join(p.get("service_interest") or []) or None),
        ("Problem", p.get("problem")),
        ("Goal", p.get("goal")),
        ("Today it costs them", p.get("current_cost")),
        ("Budget", budget),
        ("Timeline", timeline),
        ("Team size", p.get("team_size")),
        ("Notes", p.get("notes")),
        ("Score", f"{lead.score}/100 ({lead.tier})"),
        ("Came from", ", ".join(f"{k}: {v}" for k, v in conversation.source.items() if v) or None),
        ("Visitor time zone", conversation.timezone),
    ]
    return "\n".join(f"{k}: {v}" for k, v in rows if v)


def alert_team(ctx: ToolContext, subject: str, intro: str) -> None:
    s = get_settings()
    if not s.team_email:
        return
    body = f"{intro}\n\n{lead_summary(ctx.conversation, ctx.lead)}\n\nConversation:\n\n{transcript(ctx.items)}\n"
    mailer.send_later(s.team_email, subject, body, reply_to=ctx.lead.profile.get("email"))


def _mark_alerted(ctx: ToolContext) -> None:
    """Remember the team has this lead, and in which turn it was told."""
    ctx.conversation.state = {**ctx.conversation.state, "team_alerted": True, "alerted_turn": ctx.conversation.user_turns}


def _who(lead: ChatLead) -> str:
    p = lead.profile
    return p.get("name") or p.get("company") or p.get("email") or "a website visitor"


# ------------------------------------------------------------ update_lead --


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(None, max_length=80)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=30)
    company: str | None = Field(None, max_length=120)
    website: str | None = Field(None, max_length=200)
    industry: str | None = Field(None, max_length=80)
    location: str | None = Field(None, max_length=80)
    role: str | None = Field(None, max_length=80)
    service_interest: list[str] | None = None
    problem: str | None = Field(None, max_length=500)
    goal: str | None = Field(None, max_length=300)
    current_cost: str | None = Field(None, max_length=300)
    budget: Literal["under_700", "700_2500", "2500_8000", "8000_plus", "unsure"] | None = None
    timeline: Literal["asap", "1_3_months", "3_plus_months", "exploring"] | None = None
    decision_maker: bool | None = None
    team_size: str | None = Field(None, max_length=40)
    notes: str | None = Field(None, max_length=500)


async def update_lead(ctx: ToolContext, args: LeadUpdate) -> dict[str, Any]:
    updates = args.model_dump(exclude_none=True)
    if not updates:
        raise ToolFailure("Pass at least one new fact about the visitor.")
    profile = dict(ctx.lead.profile)

    if "service_interest" in updates:
        known = set(knowledge.service_ids())
        merged = set(profile.get("service_interest") or []) | {s for s in updates["service_interest"] if s in known}
        updates["service_interest"] = sorted(merged)
    if "notes" in updates and profile.get("notes"):
        updates["notes"] = f"{profile['notes']}; {updates['notes']}"[:1000]
    if "email" in updates:
        updates["email"] = updates["email"].lower()
        ctx.lead.email = updates["email"]

    profile.update(updates)
    ctx.lead.profile = profile
    a = _rescore(ctx)

    if profile.get("email") or profile.get("phone"):
        await crm.capture_visitor(ctx.db, "chatbot", ctx.conversation.id, profile, a.tier == "hot")

    # The team hears about every lead that leaves a way to reach them, not
    # only hot ones: small budgets get a personal follow-up too.
    channels = " and ".join(label for key, label in (("phone", "WhatsApp/phone"), ("email", "email")) if profile.get(key))
    if channels and a.tier == "hot" and not ctx.lead.hot_alert_sent_at:
        ctx.lead.hot_alert_sent_at = utcnow()
        _mark_alerted(ctx)
        alert_team(ctx, f"Hot lead: {_who(ctx.lead)} ({a.score}/100)", "A hot lead is chatting on scalryx.com right now.")
    elif channels and not ctx.conversation.state.get("team_alerted"):
        _mark_alerted(ctx)
        alert_team(
            ctx,
            f"New lead to follow up: {_who(ctx.lead)} ({a.tier}, {a.score}/100)",
            f"A visitor left their {channels} in the chat on scalryx.com and expects to hear from the team, "
            f"usually within 2 hours.",
        )

    # Score and tier stay server-side; the model only needs the next move.
    return {"saved": sorted(updates), "next_action": a.next_action, "missing": a.missing}


# ---------------------------------------------------------- calculate_roi --


class RoiInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours_per_week: float = Field(gt=0, le=400)
    hourly_cost: float = Field(gt=0, le=2000)
    share_automated: float = Field(0.6, ge=0.1, le=0.9)
    currency: str = Field("USD", min_length=3, max_length=3)
    package: str | None = None


WORK_WEEKS = 48


async def calculate_roi(ctx: ToolContext, args: RoiInput) -> dict[str, Any]:
    weekly_hours = args.hours_per_week * args.share_automated
    weekly_value = weekly_hours * args.hourly_cost
    result: dict[str, Any] = {
        "hours_saved_per_week": round(weekly_hours, 1),
        "hours_saved_per_year": round(weekly_hours * WORK_WEEKS),
        "value_per_year": round(weekly_value * WORK_WEEKS),
        "currency": args.currency.upper(),
        "assumptions": f"{round(args.share_automated * 100)}% of the work automated, {WORK_WEEKS} working weeks a year",
    }
    tier = knowledge.tier(args.package) if args.package else None
    if tier and (price := _price(tier)) and weekly_value > 0:
        result["package"] = tier["name"]
        result["package_price"] = tier["price"]["amount"]
        result["payback_weeks"] = max(1, round(price / weekly_value))
    ctx.emit("roi", result)
    return result


# ------------------------------------------------------ check_availability --


class AvailabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days_ahead: int = Field(7, ge=1, le=21)
    part_of_day: Literal["any", "morning", "afternoon", "evening"] = "any"
    timezone: str | None = None


# Local hours the agent offers. The calendar is open around the clock, but
# nobody should be offered a 2 AM call; the booker on /contact shows the rest.
PARTS = {"any": (8, 21), "morning": (8, 12), "afternoon": (12, 17), "evening": (17, 21)}
MAX_SLOTS = 6


def pick_slots(slots: list[datetime], tz: str, part: str) -> list[datetime]:
    """Up to two well spaced options per day, within the visitor's waking
    hours, so the choice stays easy."""
    local = zone(tz) or timezone.utc
    lo, hi = PARTS.get(part, PARTS["any"])
    slots = [s for s in slots if lo <= s.astimezone(local).hour < hi]
    by_day: dict[Any, list[datetime]] = {}
    for s in slots:
        by_day.setdefault(s.astimezone(local).date(), []).append(s)
    picked: list[datetime] = []
    for day_slots in by_day.values():
        picks = [day_slots[0]]
        later = [s for s in day_slots if (s - day_slots[0]).total_seconds() >= 3 * 3600]
        if later:
            picks.append(later[len(later) // 2])
        picked.extend(picks)
        if len(picked) >= MAX_SLOTS:
            break
    return picked[:MAX_SLOTS]


async def check_availability(ctx: ToolContext, args: AvailabilityInput) -> dict[str, Any]:
    if not get_settings().calendar_enabled:
        return {"available": False, "message": "Share the booking link so they can pick a time.", **_fallback_links()}

    tz = args.timezone if zone(args.timezone) else ctx.tz
    if tz != ctx.conversation.timezone and zone(tz):
        ctx.conversation.timezone = tz
    start, end = calcom.window(args.days_ahead)
    try:
        slots = await calcom.get_slots(start, end, tz)
    except calcom.CalError as exc:
        raise ToolFailure(f"{exc} Share the booking link instead: {_fallback_links()['booking_link']}") from exc

    chosen = pick_slots(slots, tz, args.part_of_day)
    if not chosen:
        return {"available": False, "message": "No open times in that window. Try more days or another part of the day.", **_fallback_links()}

    offered = [{"start": s.astimezone(timezone.utc).isoformat(), "label": slot_label(s, tz)} for s in chosen]
    ctx.conversation.state = {**ctx.conversation.state, "offered_slots": offered}
    ctx.emit("slots", {"timezone": tz, "slots": offered})
    return {"available": True, "timezone": tz, "duration_minutes": 30, "slots": offered}


# ------------------------------------------------------------ book_meeting --


class BookingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    start: str
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    notes: str = Field(min_length=10, max_length=1000)
    phone: str | None = Field(None, max_length=30)


async def book_meeting(ctx: ToolContext, args: BookingInput) -> dict[str, Any]:
    s = get_settings()
    if not s.calendar_enabled:
        raise ToolFailure(f"In-chat booking is not set up. Share this link instead: {_fallback_links()['booking_link']}")
    if ctx.lead.booked:
        raise ToolFailure("This visitor already has a call booked. Confirm that one instead of booking another.")

    offered = {o["start"]: o for o in ctx.conversation.state.get("offered_slots", [])}
    try:
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ToolFailure("`start` must be one of the ISO times from check_availability.") from exc
    if start.isoformat() not in offered:
        raise ToolFailure("That time was not offered. Call check_availability and use one of its `start` values.")

    email = str(args.email).lower()
    try:
        booked = await calcom.create_booking(
            start,
            name=args.name,
            email=email,
            tz=ctx.tz,
            notes=args.notes,
            metadata={"source": "scalryx-agent", "conversation": ctx.conversation.id},
        )
    except calcom.CalError as exc:
        raise ToolFailure(f"{exc} Call check_availability again and offer fresh times.") from exc

    ctx.db.add(
        Booking(
            conversation_id=ctx.conversation.id,
            cal_uid=booked.uid,
            start=booked.start,
            end=booked.end,
            location=booked.location,
            attendee_name=args.name,
            attendee_email=email,
        )
    )
    profile = {**ctx.lead.profile, "name": args.name, "email": email}
    if args.phone:
        profile["phone"] = args.phone
    ctx.lead.profile, ctx.lead.email, ctx.lead.booked = profile, email, True
    _rescore(ctx)

    when = slot_label(booked.start, ctx.tz)
    ctx.emit("booking", {"label": when, "timezone": ctx.tz, "start": booked.start.isoformat(), "location": booked.location, "email": email})
    alert_team(
        ctx,
        f"Call booked: {_who(ctx.lead)}, {slot_label(booked.start, s.owner_timezone)}",
        f"A growth call was booked from the chat for {slot_label(booked.start, s.owner_timezone)} ({s.owner_timezone}).\n"
        f"Meeting: {booked.location or 'see Cal.com'}\nTheir brief: {args.notes}",
    )
    return {"booked": True, "when": when, "timezone": ctx.tz, "meeting_link": booked.location, "invite_sent_to": email}


# ------------------------------------------------------ send_project_brief --


class BriefInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=20, max_length=1500)
    recommended_package: str
    next_steps: list[str] = Field(min_length=1, max_length=5)


async def send_project_brief(ctx: ToolContext, args: BriefInput) -> dict[str, Any]:
    p = ctx.lead.profile
    if not p.get("email"):
        raise ToolFailure("Ask for their email first, and save it with update_lead.")
    if ctx.lead.brief_sent_at:
        raise ToolFailure("The brief was already sent in this conversation.")

    s, site = get_settings(), knowledge.load()["site"]
    tier = knowledge.tier(args.recommended_package)
    package = (
        f"Recommended package: {tier['name']} ({knowledge.price_line(tier)}). {tier['promise']}."
        if tier
        else "Package: we'll recommend one on the call, once we know a little more."
    )
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(args.next_steps, 1))
    booking = s.cal_booking_url or f"{site['url']}/contact"
    text = (
        f"Hi {p.get('name') or 'there'},\n\n"
        f"Thanks for chatting with us on scalryx.com. Here's the brief we put together.\n\n"
        f"{args.summary}\n\n{package}\n\nNext steps:\n{steps}\n\n"
        f"Book your free 30 minute growth call: {booking}\n"
        f"Or just reply to this email. {site['responseTime']}.\n\n"
        f"The Scalryx team\n{site['url']}\n"
    )
    sent = await mailer.send(
        p["email"], "Your Scalryx project brief", text, reply_to=s.team_email or site["email"], bcc=s.team_email or None
    )
    if not sent:
        return {"sent": False, "message": f"Email could not be sent right now. Tell them the team will follow up at {p['email']}."}
    ctx.lead.brief_sent_at = utcnow()
    return {"sent": True, "to": p["email"]}


# -------------------------------------------------------- handoff_to_human --


class HandoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=500)
    urgency: Literal["normal", "high"] = "normal"


async def handoff_to_human(ctx: ToolContext, args: HandoffInput) -> dict[str, Any]:
    prefix = "URGENT: " if args.urgency == "high" else ""
    # A lead alert sent this same turn already carries the whole chat; don't
    # email the team twice. Urgent handoffs always go out.
    already = ctx.conversation.state.get("alerted_turn") == ctx.conversation.user_turns
    if args.urgency == "high" or not already:
        alert_team(ctx, f"{prefix}Chat handoff: {_who(ctx.lead)}", f"Scout handed this conversation to the team.\nReason: {args.reason}")
    site = knowledge.load()["site"]
    return {
        "team_notified": bool(get_settings().team_email),
        "response_time": site["responseTime"],
        "email": site["email"],
        "whatsapp": site["whatsapp"],
        "note": "Ask for their email or WhatsApp if you don't have it, so the team can reply.",
    }


# -------------------------------------------------------------- registry --


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def definitions() -> list[dict[str, Any]]:
    """Function tools for the Responses API. Deterministic, so the prompt
    cache holds. Not strict: most fields are optional, and every input is
    validated by the pydantic models below anyway."""
    text = lambda d: {"type": "string", "description": d}  # noqa: E731
    tiers = knowledge.tier_ids()
    defs = [
        {
            "name": "update_lead",
            "description": (
                "Save facts the visitor has just shared about themselves or their business. Call it in the same turn "
                "the visitor reveals anything new; pass only the new facts. Returns next_action (what to do next) and "
                "missing (what is still unknown, in the order to ask). Never mention scores or ratings to the visitor."
            ),
            "parameters": _schema(
                {
                    "name": text("Their first name or full name."),
                    "email": text("Their email address, exactly as typed."),
                    "phone": text("WhatsApp or phone number, exactly as given, with the country code if they gave one."),
                    "company": text("Business or company name."),
                    "website": text("Their current website, if any."),
                    "industry": text("What kind of business they run, in their words. Only what they told you, never a guess, and not the thing they want built."),
                    "location": text("City and/or country of the business."),
                    "role": text("Their role, e.g. owner, marketing manager."),
                    "service_interest": {
                        "type": "array",
                        "items": {"type": "string", "enum": knowledge.service_ids()},
                        "description": "Which of our services fit what they want.",
                    },
                    "problem": text("The problem or pain in their words, one or two sentences."),
                    "goal": text("The outcome they want."),
                    "current_cost": text("How it's handled today and what that costs: hours, money, missed leads."),
                    "budget": {
                        "type": "string",
                        "enum": ["under_700", "700_2500", "2500_8000", "8000_plus", "unsure"],
                        "description": (
                            "Budget in USD: under_700 = below $700, 700_2500 = $700 to $2,500, 2500_8000 = $2,500 to $8,000, "
                            "8000_plus = above $8,000, unsure = they gave no figure. Map any stated amount to its range."
                        ),
                    },
                    "timeline": {
                        "type": "string",
                        "enum": ["asap", "1_3_months", "3_plus_months", "exploring"],
                        "description": "When they want to start.",
                    },
                    "decision_maker": {"type": "boolean", "description": "Whether they make the buying decision."},
                    "team_size": text("Team or company size."),
                    "notes": text("Anything else the team should know before a call."),
                }
            ),
        },
        {
            "name": "calculate_roi",
            "description": (
                "Work out the time and money an automation or AI assistant would save, from numbers the visitor gave. "
                "Use the result in one concrete sentence. Never guess the inputs. Also save the numbers with update_lead as current_cost."
            ),
            "parameters": _schema(
                {
                    "hours_per_week": {"type": "number", "description": "Hours a week spent on the manual work today."},
                    "hourly_cost": {"type": "number", "description": "Cost of one hour of that work, in the visitor's currency."},
                    "share_automated": {
                        "type": "number",
                        "description": "Share of the work automation could take over, 0.1 to 0.9. Default 0.6.",
                    },
                    "currency": text("ISO currency code. Default USD."),
                    "package": {"type": "string", "enum": tiers, "description": "Package to compute the payback period against."},
                },
                ["hours_per_week", "hourly_cost"],
            ),
        },
        {
            "name": "check_availability",
            "description": (
                "Get real open times for the free 30 minute growth call. The chat shows the returned times as buttons. "
                "Call it when next_action is offer_call or the visitor asks to book."
            ),
            "parameters": _schema(
                {
                    "days_ahead": {"type": "integer", "description": "How many days ahead to look, 1 to 21. Default 7."},
                    "part_of_day": {"type": "string", "enum": ["any", "morning", "afternoon", "evening"]},
                    "timezone": text("IANA time zone, only if the visitor named a different one than in the context."),
                }
            ),
        },
        {
            "name": "book_meeting",
            "description": (
                "Book the free growth call once the visitor has picked a time and given their name and email. "
                "Cal.com emails them the invite."
            ),
            "parameters": _schema(
                {
                    "start": text("The chosen slot's `start`, exactly as check_availability returned it."),
                    "name": text("The visitor's name."),
                    "email": text("The visitor's email."),
                    "notes": text("Two or three sentences for the team: business, problem, goal, budget, timeline."),
                    "phone": text("Phone or WhatsApp, if they gave one."),
                },
                ["start", "name", "email", "notes"],
            ),
        },
        {
            "name": "send_project_brief",
            "description": (
                "Email the visitor a short written brief of their project with the recommended package and next steps. "
                "Only after they agreed to receive it and their email is saved."
            ),
            "parameters": _schema(
                {
                    "summary": text("Three to five sentences in their terms: the business, the problem, what we'd build, the expected result."),
                    "recommended_package": {"type": "string", "enum": [*tiers, "not_sure"]},
                    "next_steps": {"type": "array", "items": {"type": "string"}, "description": "One to five short next steps."},
                },
                ["summary", "recommended_package", "next_steps"],
            ),
        },
        {
            "name": "handoff_to_human",
            "description": (
                "Alert the Scalryx team to take over: the visitor asks for a person, has a complaint or an existing "
                "project issue, needs a custom quote, or it's urgent."
            ),
            "parameters": _schema(
                {"reason": text("Why the team should step in, in one sentence."), "urgency": {"type": "string", "enum": ["normal", "high"]}},
                ["reason"],
            ),
        },
    ]
    return [{"type": "function", **d, "strict": False} for d in defs]


Handler = Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]

HANDLERS: dict[str, tuple[type[BaseModel], Handler]] = {
    "update_lead": (LeadUpdate, update_lead),
    "calculate_roi": (RoiInput, calculate_roi),
    "check_availability": (AvailabilityInput, check_availability),
    "book_meeting": (BookingInput, book_meeting),
    "send_project_brief": (BriefInput, send_project_brief),
    "handoff_to_human": (HandoffInput, handoff_to_human),
}


async def run(ctx: ToolContext, name: str, tool_input: Any) -> tuple[str, bool]:
    """Run one tool call. Returns (result JSON, is_error)."""
    if name not in HANDLERS:
        return json.dumps({"error": f"Unknown tool {name}."}), True
    model, handler = HANDLERS[name]
    try:
        args = model.model_validate(tool_input if isinstance(tool_input, dict) else {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
        return json.dumps({"error": f"Invalid input: {problems}"}), True
    try:
        return json.dumps(await handler(ctx, args)), False
    except ToolFailure as exc:
        return json.dumps({"error": str(exc)}), True
