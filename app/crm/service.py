"""Lead logic shared by the REST routes, the ingest endpoint and the
chatbot hook. Functions flush but never commit: the caller owns the
transaction."""

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..db import utcnow
from .models import Activity, Lead
from .schemas import CLOSED, STATUSES, IngestIn, LeadCreate, LeadUpdate

SORTABLE = {
    "created_at": Lead.created_at,
    "updated_at": Lead.updated_at,
    "business_name": Lead.business_name,
    "status": Lead.status,
    "next_follow_up_at": Lead.next_follow_up_at,
    "is_hot": Lead.is_hot,
}


def label(value: str) -> str:
    return value.replace("_", " ")


# ------------------------------------------------------------------ dates --


def owner_zone() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(get_settings().owner_timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


def day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Start and end of today in the owner's time zone, as UTC."""
    local = (now or utcnow()).astimezone(owner_zone())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def phone_key(phone: str | None) -> str | None:
    """Last ten digits, so +1 (555) 010-2000 and 5550102000 match."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] or None


# ---------------------------------------------------------------- queries --


@dataclass
class Filters:
    q: str | None = None
    status: list[str] | None = None
    source: str | None = None
    is_hot: bool | None = None
    follow_up: str | None = None  # today | overdue | due


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def apply_filters(stmt: Select, f: Filters) -> Select:
    if f.q and f.q.strip():
        pattern = _like(f.q.strip())
        terms = [
            col.ilike(pattern, escape="\\") for col in (Lead.business_name, Lead.contact_name, Lead.phone, Lead.email)
        ]
        digits = re.sub(r"\D", "", f.q)
        if len(digits) >= 3:
            terms.append(Lead.phone_key.ilike(_like(digits), escape="\\"))
        stmt = stmt.where(or_(*terms))
    if f.status:
        stmt = stmt.where(Lead.status.in_(f.status))
    if f.source:
        stmt = stmt.where(Lead.source == f.source)
    if f.is_hot is not None:
        stmt = stmt.where(Lead.is_hot == f.is_hot)
    if f.follow_up:
        start, end = day_bounds()
        window = {
            "today": and_(Lead.next_follow_up_at >= start, Lead.next_follow_up_at < end),
            "overdue": Lead.next_follow_up_at < start,
            "due": Lead.next_follow_up_at < end,
        }[f.follow_up]
        stmt = stmt.where(window, Lead.status.not_in(CLOSED))
    return stmt


async def list_leads(
    db: AsyncSession, f: Filters, sort: str, descending: bool, page: int, page_size: int
) -> tuple[list[Lead], int]:
    total = await db.scalar(apply_filters(select(func.count(Lead.id)), f)) or 0
    col = SORTABLE[sort]
    order = col.desc() if descending else col.asc()
    if sort == "next_follow_up_at":
        order = order.nulls_last()
    stmt = apply_filters(select(Lead), f).order_by(order, Lead.id.desc()).limit(page_size).offset((page - 1) * page_size)
    return list((await db.scalars(stmt)).all()), total


async def get_with_timeline(db: AsyncSession, lead_id: int) -> Lead | None:
    return await db.get(Lead, lead_id, populate_existing=True, options=[selectinload(Lead.activities)])


# ------------------------------------------------------------------ writes --


def log(db: AsyncSession, lead: Lead, type_: str, content: str) -> Activity:
    activity = Activity(lead_id=lead.id, type=type_, content=content)
    db.add(activity)
    return activity


async def create_lead(db: AsyncSession, data: LeadCreate) -> Lead:
    lead = Lead(**data.model_dump(), phone_key=phone_key(data.phone))
    db.add(lead)
    await db.flush()
    log(db, lead, "note", f"Lead added ({label(lead.source)})")
    await db.flush()
    return lead


async def update_lead(db: AsyncSession, lead: Lead, data: LeadUpdate) -> Lead:
    changes = data.model_dump(exclude_unset=True)
    old_status = lead.status
    for name, value in changes.items():
        setattr(lead, name, value)
    if "phone" in changes:
        lead.phone_key = phone_key(lead.phone)
    if lead.status != "lost":
        lead.lost_reason = None
    if lead.status != old_status:
        log(db, lead, "status_change", f"{label(old_status)} → {label(lead.status)}")
    lead.updated_at = utcnow()
    await db.flush()
    return lead


async def add_activity(db: AsyncSession, lead: Lead, type_: str, content: str) -> Activity:
    activity = log(db, lead, type_, content)
    lead.updated_at = utcnow()
    await db.flush()
    return activity


async def find_duplicate(
    db: AsyncSession, email: str | None, phone: str | None, ref: str | None, source: str
) -> Lead | None:
    clauses = []
    if ref:
        clauses.append(and_(Lead.external_ref == ref, Lead.source == source))
    if email:
        clauses.append(Lead.email == email)
    key = phone_key(phone)
    if key:
        clauses.append(Lead.phone_key == key)
    if not clauses:
        return None
    return await db.scalar(select(Lead).where(or_(*clauses)).order_by(Lead.id).limit(1))


async def upsert_from_ingest(db: AsyncSession, data: IngestIn) -> tuple[Lead, bool]:
    """Create a lead from the chatbot or a form, or add to the one that
    already has this email or phone. Returns (lead, created)."""
    ref = (data.external_ref or "")[:64] or None
    lead = await find_duplicate(db, data.email, data.phone, ref, data.source)
    # One line per conversation, so repeat calls replace it instead of piling up.
    marker = f"[{data.source}:{ref[:8]}]" if ref else f"[{data.source}]"
    note = f"{marker} {' '.join(data.notes.split())}" if data.notes else None

    if lead is None:
        lead = Lead(
            business_name=data.business_name or data.name or data.email or data.phone or "Unknown",
            contact_name=data.name,
            phone=data.phone,
            phone_key=phone_key(data.phone),
            email=data.email,
            website=data.website,
            industry=data.industry,
            city=data.city,
            country=data.country,
            source=data.source,
            status="new",
            is_hot=data.is_hot,
            notes=note or "",
            external_ref=ref,
        )
        db.add(lead)
        await db.flush()
        log(db, lead, "note", f"Captured via {label(data.source)}")
        await db.flush()
        return lead, True

    # Existing lead: fill gaps, never overwrite what was entered by hand.
    for field, value in (
        ("contact_name", data.name),
        ("phone", data.phone),
        ("email", data.email),
        ("website", data.website),
        ("industry", data.industry),
        ("city", data.city),
        ("country", data.country),
        ("external_ref", ref),
    ):
        if value and not getattr(lead, field):
            setattr(lead, field, value)
    lead.phone_key = phone_key(lead.phone)
    if data.is_hot:
        lead.is_hot = True
    if note:
        existing = re.search(rf"^{re.escape(marker)}.*$", lead.notes, re.M) if ref else None
        if note in lead.notes:
            pass
        elif existing:
            lead.notes = lead.notes[: existing.start()] + note + lead.notes[existing.end() :]
        else:
            lead.notes = f"{lead.notes}\n\n{note}".strip()
            log(db, lead, "note", f"Got in touch again via {label(data.source)}")
    lead.updated_at = utcnow()
    await db.flush()
    return lead, False


# ------------------------------------------------------------------ stats --


async def stats(db: AsyncSession) -> dict:
    start, end = day_bounds()
    local_today = start.astimezone(owner_zone())
    week_start = (local_today - timedelta(days=local_today.weekday())).astimezone(timezone.utc)
    month_start = local_today.replace(day=1).astimezone(timezone.utc)
    open_ = Lead.status.not_in(CLOSED)

    counts = dict((await db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status))).all())

    async def count(*where) -> int:
        return await db.scalar(select(func.count(Lead.id)).where(*where)) or 0

    return {
        "total": sum(counts.values()),
        "by_status": {s: counts.get(s, 0) for s in STATUSES},
        "hot": await count(Lead.is_hot, open_),
        "follow_ups_today": await count(open_, Lead.next_follow_up_at >= start, Lead.next_follow_up_at < end),
        "overdue": await count(open_, Lead.next_follow_up_at < start),
        "added_this_week": await count(Lead.created_at >= week_start),
        "won_this_month": await count(Lead.status == "won", Lead.updated_at >= month_start),
    }


# -------------------------------------------------------------------- csv --

CSV_COLUMNS = [
    "id", "business_name", "contact_name", "phone", "email", "website", "industry", "city", "country",
    "source", "status", "is_hot", "next_follow_up_at", "lost_reason", "notes", "created_at", "updated_at",
]  # fmt: skip


def _cell(value: object) -> str:
    text = "" if value is None else value.isoformat() if isinstance(value, datetime) else str(value)
    # A cell starting with = + - @ would run as a formula when opened in Excel.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def export_csv(db: AsyncSession, f: Filters) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    rows = await db.scalars(apply_filters(select(Lead), f).order_by(Lead.id))
    for lead in rows:
        writer.writerow([_cell(getattr(lead, c)) for c in CSV_COLUMNS])
    return out.getvalue()


# ------------------------------------------------------- chatbot / forms --


async def capture_visitor(
    db: AsyncSession, source: str, ref: str, profile: dict, hot: bool
) -> None:
    """Mirror a chatbot or contact-form visitor into the dashboard. The chat
    must never break because of this, so problems are logged (without lead
    data) and swallowed."""
    import logging

    summary = "; ".join(
        f"{k}: {profile[k]}"
        for k in ("service_interest", "problem", "goal", "budget", "timeline", "notes")
        if profile.get(k)
    )
    try:
        data = IngestIn(
            name=profile.get("name"),
            business_name=profile.get("company"),
            email=profile.get("email"),
            phone=profile.get("phone"),
            website=profile.get("website"),
            industry=profile.get("industry"),
            city=profile.get("location"),
            notes=summary[:2000] or None,
            is_hot=hot,
            source=source,
            external_ref=ref,
        )
        async with db.begin_nested():
            await upsert_from_ingest(db, data)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("scalryx.crm").warning("Could not save visitor to the dashboard: %s", type(exc).__name__)
