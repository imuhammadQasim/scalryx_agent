"""Database models. SQLite by default; set DATABASE_URL for Postgres."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, datetime: DateTime(timezone=True)}


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Where the visitor came from: first page, referrer and UTM tags.
    source: Mapped[dict[str, Any]] = mapped_column(default=dict)
    user_turns: Mapped[int] = mapped_column(Integer, default=0)
    # Agent scratch state, e.g. the slots last offered.
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Message(Base):
    """One Responses API input item (a visitor message, a model output item, a
    tool result), stored exactly as sent or received. The conversation is
    replayed from these rows, unchanged, on every turn."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    # The item's role for messages, otherwise its type (function_call, ...).
    kind: Mapped[str] = mapped_column(String(32))
    item: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), unique=True)
    email: Mapped[str | None] = mapped_column(String(254), index=True)
    profile: Mapped[dict[str, Any]] = mapped_column(default=dict)
    score: Mapped[int] = mapped_column(Integer, default=0)
    tier: Mapped[str] = mapped_column(String(8), default="cold")
    booked: Mapped[bool] = mapped_column(Boolean, default=False)
    hot_alert_sent_at: Mapped[datetime | None]
    brief_sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    cal_uid: Mapped[str | None] = mapped_column(String(128))
    start: Mapped[datetime]
    end: Mapped[datetime | None]
    location: Mapped[str | None] = mapped_column(String(500))
    attendee_name: Mapped[str] = mapped_column(String(120))
    attendee_email: Mapped[str] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


engine = create_async_engine(get_settings().database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    url = engine.url
    if url.get_backend_name() == "sqlite" and url.database:
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_lead(db: AsyncSession, conversation_id: str) -> Lead:
    lead = await db.scalar(select(Lead).where(Lead.conversation_id == conversation_id))
    if lead is None:
        lead = Lead(conversation_id=conversation_id, profile={})
        db.add(lead)
        await db.flush()
    return lead
