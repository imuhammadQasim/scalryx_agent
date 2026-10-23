"""Contact-form enquiries become scored leads, reach the team, and the
visitor gets a confirmation."""

from app.config import Settings, get_settings
from app.integrations import mailer
from tests.test_chat import HEADERS, client  # noqa: F401

ADMIN = {"Authorization": "Bearer admin-token"}

FORM = {
    "name": "Sara Khan",
    "email": "Sara@SmileClinic.ae",
    "company": "Smile Clinic",
    "project_type": "ai",
    "project_label": "An AI assistant or chatbot",
    "budget": "2500_8000",
    "message": "We miss a lot of calls at the front desk and want a bot that books appointments.",
    "page": "/contact",
    "timezone": "Asia/Dubai",
}


def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_later", lambda to, subject, text, reply_to=None: sent.append((to, subject, text, reply_to)))
    return sent


def test_enquiry_becomes_lead_alerts_team_and_confirms_once(client, monkeypatch):  # noqa: F811
    sent = capture(monkeypatch)
    monkeypatch.setattr(get_settings(), "owner_email", "owner@scalryx.com")

    res = client.post("/v1/enquiries", headers=HEADERS, json=FORM)
    assert res.status_code == 200 and res.json()["ok"]

    lead = next(l for l in client.get("/v1/admin/leads", headers=ADMIN).json() if l["conversation_id"] == res.json()["id"])
    assert lead["profile"]["email"] == "sara@smileclinic.ae"
    assert lead["profile"]["service_interest"] == ["ai-assistants", "automations"]
    assert lead["tier"] in {"warm", "hot"}

    (team_to, team_subject, team_text, team_reply), (visitor_to, visitor_subject, visitor_text, visitor_reply) = sent
    assert team_to == ["owner@scalryx.com"] and team_reply == "sara@smileclinic.ae"
    assert "Sara Khan (Smile Clinic)" in team_subject and "front desk" in team_text
    assert visitor_to == "sara@smileclinic.ae" and visitor_reply == "owner@scalryx.com"
    assert visitor_text.startswith("Hi Sara,") and "front desk" in visitor_text and "30 minute call" in visitor_text

    # A second form with the same address within the hour: team alert only.
    client.post("/v1/enquiries", headers=HEADERS, json=FORM)
    assert [to for to, *_ in sent[2:]] == [["owner@scalryx.com"]]


def test_enquiry_rejects_bad_input_and_missing_key(client):  # noqa: F811
    assert client.post("/v1/enquiries", json=FORM).status_code == 401
    assert client.post("/v1/enquiries", headers=HEADERS, json={**FORM, "email": "nope"}).status_code == 422
    assert client.post("/v1/enquiries", headers=HEADERS, json={**FORM, "message": "too short"}).status_code == 422


def test_gmail_needs_only_user_and_password():
    s = Settings(_env_file=None, smtp_user="me@gmail.com", smtp_password="abcd efgh ijkl mnop")
    assert s.email_enabled and s.smtp_host == "smtp.gmail.com"
    assert s.mail_from == "Scalryx <me@gmail.com>" and s.team_email == "me@gmail.com"

    custom = Settings(_env_file=None, smtp_user="me@gmail.com", smtp_from="Hello <hi@scalryx.com>", owner_email="team@scalryx.com")
    assert custom.mail_from == "Hello <hi@scalryx.com>" and custom.team_email == "team@scalryx.com"
    assert not Settings(_env_file=None).email_enabled


def test_several_team_inboxes_all_get_the_alert(client, monkeypatch):  # noqa: F811
    sent = capture(monkeypatch)
    monkeypatch.setattr(get_settings(), "owner_email", "ahad@scalryx.com, muhammad.qasim.dev07@gmail.com")
    assert get_settings().team_emails == ["ahad@scalryx.com", "muhammad.qasim.dev07@gmail.com"]

    res = client.post("/v1/enquiries", headers=HEADERS, json={**FORM, "email": "other@example.com"})
    assert res.status_code == 200
    assert sent[0][0] == ["ahad@scalryx.com", "muhammad.qasim.dev07@gmail.com"]
    assert sent[1][0] == "other@example.com" and sent[1][3] == "ahad@scalryx.com"
