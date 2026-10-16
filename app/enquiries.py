"""Contact-form enquiries from the website. Each one becomes a scored lead in
the same table as chat leads, and the team gets an email straight away."""

import secrets
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from . import knowledge
from .crm import service as crm
from .config import get_settings
from .db import Conversation, ChatLead, Message, SessionLocal
from .integrations import mailer
from .scoring import assess
from .tools import lead_summary, zone

# The form's "What do you need?" values, mapped to the agent's service ids.
PROJECT_SERVICES = {
    "website": ["websites"],
    "shopify": ["online-stores"],
    "ecommerce": ["online-stores"],
    "ai": ["ai-assistants", "automations"],
    "saas": ["apps-and-software"],
    "mobile": ["apps-and-software"],
    "cloud": ["care"],
    "team": ["apps-and-software"],
}


class EnquiryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    company: str | None = Field(None, max_length=120)
    project_type: str = Field(max_length=40)
    project_label: str | None = Field(None, max_length=80)
    budget: Literal["under_700", "700_2500", "2500_8000", "8000_plus", "unsure"] | None = None
    message: str = Field(min_length=20, max_length=4000)
    page: str | None = Field(None, max_length=300)
    timezone: str | None = Field(None, max_length=64)
    referrer: str | None = Field(None, max_length=300)


async def save(body: EnquiryRequest, ip: str) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "name": body.name,
        "email": str(body.email).lower(),
        "company": body.company or None,
        "problem": body.message[:500],
        "service_interest": PROJECT_SERVICES.get(body.project_type, []),
        "budget": body.budget,
        "notes": f"Contact form: {body.project_label or body.project_type}",
    }
    profile = {k: v for k, v in profile.items() if v not in (None, "", [])}
    a = assess(profile)

    async with SessionLocal() as db:
        conv = Conversation(
            id=secrets.token_urlsafe(24),
            ip=ip,
            timezone=body.timezone if zone(body.timezone) else None,
            source={"channel": "contact form", "page": body.page, "referrer": body.referrer},
            state={},
            user_turns=0,
        )
        lead = ChatLead(conversation_id=conv.id, email=profile["email"], profile=profile, score=a.score, tier=a.tier, booked=False)
        db.add(conv)
        await db.flush()
        db.add(lead)
        db.add(Message(conversation_id=conv.id, kind="user", item={"role": "user", "content": body.message}))
        await crm.capture_visitor(
            db, "website_form", conv.id, {**profile, "notes": f"{body.project_label or body.project_type}"}, a.tier == "hot"
        )
        await db.commit()

    s = get_settings()
    if s.team_email:
        who = f"{body.name} ({body.company})" if body.company else body.name
        mailer.send_later(
            s.team_email,
            f"New enquiry: {who}, {a.tier} lead ({a.score}/100)",
            f"Someone sent the contact form on scalryx.com. Reply to this email to answer them.\n\n"
            f"{lead_summary(conv, lead)}\n\nTheir message:\n\n{body.message}\n",
            reply_to=profile["email"],
        )
    if _may_confirm(profile["email"]):
        site = knowledge.load()["site"]
        mailer.send_later(profile["email"], "We got your message", confirmation(body, site), reply_to=s.team_email or site["email"])
    return {"ok": True, "id": conv.id}


# Anyone can type someone else's address into a form, so each address gets
# at most one confirmation an hour. In memory: enough for one process.
CONFIRM_EVERY = 3600
_confirmed: dict[str, float] = {}


def _may_confirm(email: str) -> bool:
    now = time.monotonic()
    if email in _confirmed and now - _confirmed[email] < CONFIRM_EVERY:
        return False
    if len(_confirmed) > 10_000:
        _confirmed.clear()
    _confirmed[email] = now
    return True


def confirmation(body: EnquiryRequest, site: dict[str, Any]) -> str:
    """The visitor's copy: proof it arrived, and the quickest next step."""
    first = body.name.split()[0]
    booking = get_settings().cal_booking_url or f"{site['url']}/contact"
    return (
        f"Hi {first},\n\n"
        f"Thanks for getting in touch with Scalryx. Your message reached us, and a real person "
        f"will reply. {site['responseTime']} during working hours.\n\n"
        f"If you would rather talk it through, book a free 30 minute call here:\n{booking}\n\n"
        f"Here is what you sent:\n\n{body.message}\n\n"
        f"Just reply to this email if you want to add anything.\n\n"
        f"The Scalryx team\n{site['url']}\n"
    )
