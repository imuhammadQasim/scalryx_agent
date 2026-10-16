"""Lead dashboard API: auth, CRUD, status-change timeline, search and
filters, chatbot ingest and its de-duplication, CSV export."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.crm import security, seed, service
from app.crm.schemas import IngestIn
from app.db import SessionLocal, utcnow
from app.main import app

EMAIL, PASSWORD = "boss@scalryx.test", "correct-horse-battery"
INGEST = {"X-Ingest-Key": "ingest-secret"}


@pytest.fixture
def anon():
    with TestClient(app, base_url="http://localhost") as c:
        c.portal.call(seed.run, False)
        security.login_failures.hits.clear()
        security.ingest_limiter.hits.clear()
        yield c


@pytest.fixture
def client(anon):
    res = anon.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert res.status_code == 200
    # Every test starts from an empty table.
    for lead in anon.get("/api/leads", params={"page_size": 100}).json()["items"]:
        anon.delete(f"/api/leads/{lead['id']}")
    return anon


def add(client, **fields):
    res = client.post("/api/leads", json={"business_name": "Acme Dental", "phone": "+1 415 555 0100", **fields})
    assert res.status_code == 201, res.text
    return res.json()


# ------------------------------------------------------------------- auth --


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/leads"),
        ("post", "/api/leads"),
        ("get", "/api/leads/stats"),
        ("get", "/api/leads/export.csv"),
        ("get", "/api/leads/1"),
        ("patch", "/api/leads/1"),
        ("delete", "/api/leads/1"),
        ("post", "/api/leads/1/activities"),
        ("get", "/api/auth/me"),
    ],
)
def test_dashboard_routes_need_login(anon, method, path):
    assert getattr(anon, method)(path).status_code == 401


def test_login_sets_http_only_cookie_and_logout_ends_it(anon):
    res = anon.post("/api/auth/login", json={"email": EMAIL.upper(), "password": PASSWORD})
    assert res.status_code == 200 and res.json() == {"email": EMAIL}
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert anon.get("/api/auth/me").status_code == 200
    assert anon.post("/api/auth/logout").status_code == 204
    assert anon.get("/api/auth/me").status_code == 401


def test_wrong_password_then_lockout(anon):
    for _ in range(5):
        assert anon.post("/api/auth/login", json={"email": EMAIL, "password": "nope"}).status_code == 401
    locked = anon.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert locked.status_code == 429 and "Retry-After" in locked.headers


def test_writes_from_other_origins_are_refused(client):
    res = client.post("/api/leads", json={"business_name": "X"}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


# ------------------------------------------------------------------- CRUD --


def test_crud_and_validation(client):
    lead = add(client, email="Owner@Acme.example", status="interested", is_hot=True)
    assert lead["email"] == "owner@acme.example" and lead["status"] == "interested" and lead["is_hot"]
    assert lead["activities"][0]["content"].startswith("Lead added")

    assert client.get(f"/api/leads/{lead['id']}").json()["business_name"] == "Acme Dental"
    res = client.patch(f"/api/leads/{lead['id']}", json={"contact_name": "Dr Rauf", "website": "acme.example"})
    assert res.json()["contact_name"] == "Dr Rauf" and res.json()["website"] == "https://acme.example"
    assert client.patch(f"/api/leads/{lead['id']}", json={"email": None}).json()["email"] is None

    assert client.delete(f"/api/leads/{lead['id']}").status_code == 204
    assert client.get(f"/api/leads/{lead['id']}").status_code == 404

    bad = client.post("/api/leads", json={"business_name": "  ", "phone": "12"})
    assert bad.status_code == 422 and {"business_name", "phone"} <= set(bad.json()["errors"])
    assert client.post("/api/leads", json={"business_name": "X", "status": "weird"}).status_code == 422
    assert client.patch("/api/leads/999999", json={"notes": "x"}).status_code == 404


def test_status_change_is_logged_and_notes_are_added(client):
    lead = add(client)
    res = client.patch(f"/api/leads/{lead['id']}", json={"status": "contacted"}).json()
    assert res["activities"][0]["type"] == "status_change"
    assert res["activities"][0]["content"] == "new → contacted"

    # Saving without changing the status adds nothing.
    again = client.patch(f"/api/leads/{lead['id']}", json={"status": "contacted", "is_hot": True}).json()
    assert [a["type"] for a in again["activities"]].count("status_change") == 1

    lost = client.patch(f"/api/leads/{lead['id']}", json={"status": "lost", "lost_reason": "Too pricey"}).json()
    assert lost["lost_reason"] == "Too pricey" and lost["activities"][0]["content"] == "contacted → lost"

    note = client.post(f"/api/leads/{lead['id']}/activities", json={"type": "call", "content": "Spoke 5 min"})
    assert note.status_code == 201 and note.json()["type"] == "call"
    bad = client.post(f"/api/leads/{lead['id']}/activities", json={"type": "status_change", "content": "x"})
    assert bad.status_code == 422
    assert client.get(f"/api/leads/{lead['id']}").json()["activities"][0]["content"] == "Spoke 5 min"


def test_list_search_filter_sort_paginate_and_stats(client):
    add(client, business_name="Alpha Bakery", status="interested", is_hot=True, email="a@alpha.example")
    add(client, business_name="Beta Plumbing", phone="+44 20 7946 0958", source="referral")
    add(client, business_name="Gamma_100% Auto", status="won")

    def names(**params):
        body = client.get("/api/leads", params=params).json()
        return [i["business_name"] for i in body["items"]]

    assert names(sort="business_name", order="asc") == ["Alpha Bakery", "Beta Plumbing", "Gamma_100% Auto"]
    assert names(q="alpha") == ["Alpha Bakery"]
    assert names(q="a@alpha") == ["Alpha Bakery"]
    assert names(q="7946") == ["Beta Plumbing"]  # by digits, ignoring formatting
    assert names(q="_") == ["Gamma_100% Auto"] and names(q="%") == ["Gamma_100% Auto"]  # wildcards are literal
    assert names(is_hot="true") == ["Alpha Bakery"]
    assert names(source="referral") == ["Beta Plumbing"]
    assert sorted(names(status=["won", "interested"])) == ["Alpha Bakery", "Gamma_100% Auto"]
    page = client.get("/api/leads", params={"page_size": 2, "page": 2, "sort": "business_name", "order": "asc"}).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert client.get("/api/leads", params={"sort": "password"}).status_code == 422

    stats = client.get("/api/leads/stats").json()
    assert stats["total"] == 3 and stats["hot"] == 1 and stats["by_status"]["won"] == 1
    assert stats["added_this_week"] == 3 and stats["won_this_month"] == 1


def test_follow_up_filters_and_stats(client):
    now = utcnow()
    add(client, business_name="Late", next_follow_up_at=(now - timedelta(days=3)).isoformat())
    add(client, business_name="Soon", next_follow_up_at=(now + timedelta(days=3)).isoformat())
    add(client, business_name="Closed", status="won", next_follow_up_at=(now - timedelta(days=3)).isoformat())

    def names(follow_up):
        return [i["business_name"] for i in client.get("/api/leads", params={"follow_up": follow_up}).json()["items"]]

    assert names("overdue") == ["Late"] and names("due") == ["Late"] and names("today") == []
    assert client.get("/api/leads/stats").json()["overdue"] == 1


# ----------------------------------------------------------------- ingest --


def test_ingest_needs_the_shared_key(anon):
    body = {"name": "Sam", "email": "sam@x.example"}
    assert anon.post("/api/leads/ingest", json=body).status_code == 401
    assert anon.post("/api/leads/ingest", json=body, headers={"X-Ingest-Key": "wrong"}).status_code == 401
    assert anon.post("/api/leads/ingest", json={"name": "Sam"}, headers=INGEST).status_code == 422


def test_ingest_creates_then_deduplicates_by_email_or_phone(client):
    first = client.post(
        "/api/leads/ingest",
        headers=INGEST,
        json={"name": "Sam Lee", "business_name": "Lee Roofing", "email": "Sam@Lee.example", "notes": "Wants a website"},
    )
    assert first.status_code == 201 and first.json()["created"]
    lead_id = first.json()["id"]

    # Same email, new phone: the same lead gains the phone.
    body = {"email": "sam@lee.example", "phone": "(415) 555-0199", "is_hot": True}
    second = client.post("/api/leads/ingest", headers=INGEST, json=body)
    assert second.status_code == 200 and second.json() == {"id": lead_id, "created": False}
    # Same phone, written differently and no email.
    body = {"phone": "+1 415 555 0199", "notes": "Asked for pricing"}
    third = client.post("/api/leads/ingest", headers=INGEST, json=body)
    assert third.json() == {"id": lead_id, "created": False}

    assert client.get("/api/leads").json()["total"] == 1
    lead = client.get(f"/api/leads/{lead_id}").json()
    assert lead["source"] == "chatbot" and lead["is_hot"] and lead["phone"] == "(415) 555-0199"
    assert lead["business_name"] == "Lee Roofing"
    assert "Wants a website" in lead["notes"] and "Asked for pricing" in lead["notes"]


def test_ingest_does_not_overwrite_hand_entered_data(client):
    lead = add(client, business_name="Manual Co", contact_name="Mia", status="interested", email="mia@m.example")
    body = {"name": "Someone Else", "business_name": "Other", "email": "mia@m.example"}
    client.post("/api/leads/ingest", headers=INGEST, json=body)
    after = client.get(f"/api/leads/{lead['id']}").json()
    assert (after["business_name"], after["contact_name"], after["status"]) == ("Manual Co", "Mia", "interested")


def test_chat_updates_for_one_conversation_replace_one_note_line(client):
    async def capture(summary):
        async with SessionLocal() as db:
            profile = {"name": "Jo", "email": "jo@j.example", "problem": summary}
            await service.capture_visitor(db, "chatbot", "conv-abcdef123", profile, False)
            await db.commit()

    client.portal.call(capture, "first version")
    client.portal.call(capture, "second version")
    body = client.get("/api/leads").json()
    assert body["total"] == 1 and body["items"][0]["source"] == "chatbot"
    notes = body["items"][0]["notes"]
    assert "second version" in notes and "first version" not in notes


def test_ingest_is_rate_limited(anon, monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_rate_per_minute", 2)
    codes = [anon.post("/api/leads/ingest", headers=INGEST, json={"email": "r@r.example"}).status_code for _ in range(4)]
    assert codes[-1] == 429


# -------------------------------------------------------------------- csv --


def test_csv_export_respects_filters_and_neutralises_formulas(client):
    add(client, business_name='=HYPERLINK("http://evil")', is_hot=True)
    add(client, business_name="Plain Co")
    res = client.get("/api/leads/export.csv", params={"is_hot": "true"})
    assert res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"]
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("id,business_name") and len(lines) == 2
    assert "'=HYPERLINK" in lines[1] and "Plain Co" not in res.text


def test_ingest_model_rejects_unreachable_visitor():
    with pytest.raises(ValueError):
        IngestIn(name="Nobody")
