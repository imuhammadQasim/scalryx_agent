"""CRM tables. Not to be confused with db.ChatLead, which is the chatbot's
per-conversation scoring row."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from ..db import Base, utcnow


class UTCDateTime(TypeDecorator):
    """SQLite drops the time zone; always hand back an aware UTC datetime."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    # Digits only, so "+1 (555) 010-2000" and "15550102000" are the same lead.
    phone_key: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(254), index=True)
    website: Mapped[str | None] = mapped_column(String(300))
    industry: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(20), index=True, default="other")
    status: Mapped[str] = mapped_column(String(20), index=True, default="new")
    is_hot: Mapped[bool] = mapped_column(Boolean, index=True, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    next_follow_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    lost_reason: Mapped[str | None] = mapped_column(String(300))
    # The chat conversation or form submission this lead came from, if any.
    external_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    activities: Mapped[list["Activity"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Activity.id.desc()"
    )


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="activities")


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AdminSession(Base):
    """Server-side session. The cookie holds a random token; only its SHA-256
    is stored, so a copy of the database can't be used to log in."""

    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
