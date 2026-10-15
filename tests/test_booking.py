"""Booking flow with a fake Cal.com: only offered times can be booked."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent
from app.config import get_settings
from app.integrations import calcom
from app.main import app
from tests.test_chat import HEADERS, FakeClient, call, outputs, parse_sse, say

SLOT = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cal_api_key", "cal_test")
    monkeypatch.setattr(s, "cal_event_type_id", 42)
    booked = []

    async def get_slots(start, end, tz):
        return [SLOT, SLOT + timedelta(hours=4)]

    async def create_booking(start, name, email, tz, notes, metadata):
        booked.append((start, name, email, tz))
        return calcom.Booked(uid="bk_1", start=start, end=start + timedelta(minutes=30), location="https://cal.com/video/x")

    monkeypatch.setattr(calcom, "get_slots", get_slots)
    monkeypatch.setattr(calcom, "create_booking", create_booking)
    with TestClient(app) as c:
        c.booked = booked
        yield c


def test_offered_slot_books_and_unoffered_is_rejected(client, monkeypatch):
    note = "Dental clinic, missed calls."
    fake = FakeClient(
        [
            ([call("check_availability", {})], "completed"),
            ([say("Fri 3 PM or 7 PM?")], "completed"),
            (
                [
                    call("book_meeting", {"start": "2026-10-20T10:00:00Z", "name": "Sara", "email": "sara@clinic.com", "notes": note}, 2),
                    call("book_meeting", {"start": SLOT.isoformat(), "name": "Sara", "email": "Sara@Clinic.com", "notes": note}, 3),
                ],
                "completed",
            ),
            ([say("You're booked!", 2)], "completed"),
        ]
    )
    monkeypatch.setattr(agent, "client", lambda: fake)

    events = parse_sse(client.post("/v1/chat", headers=HEADERS, json={"message": "book a call", "timezone": "Asia/Karachi"}).text)
    slots = next(d for n, d in events if n == "slots")
    assert slots["timezone"] == "Asia/Karachi"
    assert slots["slots"][0]["label"] == "Fri, Oct 16, 3:00 PM"

    session_id = events[0][1]["id"]
    events = parse_sse(client.post("/v1/chat", headers=HEADERS, json={"message": "3 PM, Sara, sara@clinic.com", "session_id": session_id}).text)

    rejected, accepted = outputs(fake.requests[3])[-2:]
    assert "not offered" in rejected["output"]
    assert json.loads(accepted["output"])["booked"] is True
    assert client.booked == [(SLOT, "Sara", "sara@clinic.com", "Asia/Karachi")]

    booking = next(d for n, d in events if n == "booking")
    assert booking["label"] == "Fri, Oct 16, 3:00 PM" and booking["location"].startswith("https://")

    history = client.get(f"/v1/chat/{session_id}", headers=HEADERS).json()
    assert history["booking"]["label"] == "Fri, Oct 16, 3:00 PM"
