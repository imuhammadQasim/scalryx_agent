"""Cal.com API v2: read open slots and book the free growth call."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import get_settings

SLOTS_VERSION = "2024-09-04"
BOOKINGS_VERSION = "2026-02-25"


class CalError(Exception):
    """A Cal.com call failed; the message is safe to show the model."""


@dataclass
class Booked:
    uid: str | None
    start: datetime
    end: datetime | None
    location: str | None


def _headers(version: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().cal_api_key}", "cal-api-version": version}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def get_slots(start: date, end: date, tz: str) -> list[datetime]:
    """Open start times between two dates, as aware datetimes."""
    s = get_settings()
    params = {
        "eventTypeId": s.cal_event_type_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timeZone": tz,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(f"{s.cal_api_url}/slots", params=params, headers=_headers(SLOTS_VERSION))
    except httpx.HTTPError as exc:
        raise CalError("The calendar could not be reached.") from exc
    if res.status_code != 200:
        raise CalError(f"The calendar returned an error ({res.status_code}).")

    slots: list[datetime] = []
    for day in (res.json().get("data") or {}).values():
        for slot in day:
            # The docs show objects with `start`; the schema says strings.
            ts = slot.get("start") if isinstance(slot, dict) else slot
            if ts:
                slots.append(_parse(ts))
    return sorted(slots)


async def create_booking(
    start: datetime, name: str, email: str, tz: str, notes: str, metadata: dict[str, str]
) -> Booked:
    s = get_settings()
    body: dict[str, Any] = {
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eventTypeId": s.cal_event_type_id,
        "attendee": {"name": name, "email": email, "timeZone": tz, "language": "en"},
        "bookingFieldsResponses": {"notes": notes},
        "metadata": {k[:40]: v[:500] for k, v in metadata.items()},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(f"{s.cal_api_url}/bookings", json=body, headers=_headers(BOOKINGS_VERSION))
    except httpx.HTTPError as exc:
        raise CalError("The calendar could not be reached.") from exc

    payload = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    if res.status_code not in (200, 201) or payload.get("status") != "success":
        detail = (payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else None
        raise CalError(detail or f"The booking was not accepted ({res.status_code}). The time may have just been taken.")

    data = payload["data"]
    if isinstance(data, list):  # recurring event types return a list
        data = data[0]
    location = data.get("location") or data.get("meetingUrl")
    return Booked(
        uid=data.get("uid"),
        start=_parse(data["start"]),
        end=_parse(data["end"]) if data.get("end") else None,
        location=location if isinstance(location, str) else None,
    )


def window(days_ahead: int) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    return today, today + timedelta(days=days_ahead)
