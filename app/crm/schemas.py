"""Request and response models for the lead API."""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    model_validator,
)

Source = Literal["cold_call", "chatbot", "website_form", "referral", "other"]
Status = Literal["new", "contacted", "interested", "proposal_sent", "won", "lost"]
ActivityType = Literal["call", "note", "email", "status_change"]

SOURCES: tuple[str, ...] = Source.__args__  # type: ignore[attr-defined]
STATUSES: tuple[str, ...] = Status.__args__  # type: ignore[attr-defined]
CLOSED = ("won", "lost")

_email = TypeAdapter(EmailStr)


def _blank_to_none(v: object) -> object:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _phone(v: str | None) -> str | None:
    if v is None:
        return None
    digits = re.sub(r"\D", "", v)
    if not re.fullmatch(r"[0-9+()\-.\s]{5,40}", v) or not 5 <= len(digits) <= 15:
        raise ValueError("Enter a valid phone number")
    return v


def _email_addr(v: str | None) -> str | None:
    return None if v is None else str(_email.validate_python(v)).lower()


def _website(v: str | None) -> str | None:
    if v is None:
        return None
    if not re.match(r"^https?://", v, re.I):
        v = f"https://{v}"
    if not re.fullmatch(r"https?://[^\s/$.?#][^\s]*\.[^\s]{2,}", v, re.I):
        raise ValueError("Enter a valid website address")
    return v


def _max(n: int):
    def check(v: str | None) -> str | None:
        if v is not None and len(v) > n:
            raise ValueError(f"Keep this under {n} characters")
        return v

    return AfterValidator(check)


def Text(n: int):  # noqa: N802
    """Optional text: blank becomes null, longer than n is refused."""
    return Annotated[str | None, BeforeValidator(_blank_to_none), _max(n)]


Phone = Annotated[str | None, BeforeValidator(_blank_to_none), AfterValidator(_phone), _max(40)]
Email = Annotated[str | None, BeforeValidator(_blank_to_none), AfterValidator(_email_addr), _max(254)]
Website = Annotated[str | None, BeforeValidator(_blank_to_none), AfterValidator(_website), _max(300)]


class LeadFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_name: Text(120) = None
    phone: Phone = None
    email: Email = None
    website: Website = None
    industry: Text(120) = None
    city: Text(100) = None
    country: Text(100) = None
    next_follow_up_at: datetime | None = None
    lost_reason: Text(300) = None


class LeadCreate(LeadFields):
    business_name: Annotated[str, BeforeValidator(_blank_to_none), Field(min_length=1, max_length=200)]
    source: Source = "cold_call"
    status: Status = "new"
    is_hot: bool = False
    notes: Annotated[str, Field(max_length=20000)] = ""


class LeadUpdate(LeadFields):
    """Only the fields sent are changed; send null to clear an optional one."""

    business_name: Annotated[str | None, BeforeValidator(_blank_to_none), Field(min_length=1, max_length=200)] = None
    source: Source | None = None
    status: Status | None = None
    is_hot: bool | None = None
    notes: Annotated[str | None, Field(max_length=20000)] = None

    @model_validator(mode="after")
    def required_not_null(self) -> "LeadUpdate":
        for name in ("business_name", "source", "status", "is_hot", "notes"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be empty")
        return self


class IngestIn(BaseModel):
    """What the chatbot or a website form sends."""

    model_config = ConfigDict(extra="ignore")

    name: Text(120) = None
    business_name: Text(200) = None
    email: Email = None
    phone: Phone = None
    website: Website = None
    industry: Text(120) = None
    city: Text(100) = None
    country: Text(100) = None
    notes: Text(2000) = None
    is_hot: bool = False
    source: Literal["chatbot", "website_form"] = "chatbot"
    # Stable id of the conversation or submission, so repeat calls update one lead.
    external_ref: Text(64) = None

    @model_validator(mode="after")
    def reachable(self) -> "IngestIn":
        if not (self.email or self.phone):
            raise ValueError("Send at least an email or a phone number")
        return self


class ActivityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["call", "note", "email"] = "note"
    content: Annotated[str, BeforeValidator(_blank_to_none), Field(min_length=1, max_length=5000)]


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    type: ActivityType
    content: str
    created_at: datetime


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    business_name: str
    contact_name: str | None
    phone: str | None
    email: str | None
    website: str | None
    industry: str | None
    city: str | None
    country: str | None
    source: Source
    status: Status
    is_hot: bool
    notes: str
    next_follow_up_at: datetime | None
    lost_reason: str | None
    created_at: datetime
    updated_at: datetime


class LeadDetail(LeadOut):
    activities: list[ActivityOut]


class LeadPage(BaseModel):
    items: list[LeadOut]
    total: int
    page: int
    page_size: int


class IngestOut(BaseModel):
    id: int
    created: bool


class Stats(BaseModel):
    total: int
    by_status: dict[str, int]
    hot: int
    follow_ups_today: int
    overdue: int
    added_this_week: int
    won_this_month: int


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class Me(BaseModel):
    email: str
