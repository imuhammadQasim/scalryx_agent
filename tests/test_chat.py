"""End-to-end turns against a fake OpenAI Responses API: SSE events, tool
rounds, persistence, and the append-only history replayed on every request."""

import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from app import agent
from app.main import app

HEADERS = {"X-Agent-Key": "test-key"}


def say(text: str, n: int = 1) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id=f"msg_{n}",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def call(name: str, args: dict, n: int = 1) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id=f"fc_{n}", type="function_call", call_id=f"call_{n}", name=name, arguments=json.dumps(args), status="completed"
    )


class FakeStream:
    def __init__(self, output, status):
        self.output, self.status = output, status

    def __aiter__(self):
        async def gen():
            for item in self.output:
                if item.type == "message":
                    yield SimpleNamespace(type="response.output_text.delta", delta=item.content[0].text)
            yield SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(output=self.output, status=self.status, incomplete_details=None),
            )

        return gen()


class FakeClient:
    """Replays scripted responses and records every request it receives."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **params):
        self.requests.append(copy.deepcopy(params))
        output, status = self.script.pop(0)
        return FakeStream(output, status)


def parse_sse(body: str):
    events = []
    for chunk in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def outputs(request) -> list[dict]:
    return [i for i in request["input"] if i.get("type") == "function_call_output"]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_rejects_missing_agent_key(client):
    assert client.post("/v1/chat", json={"message": "hi"}).status_code == 401


def test_tool_round_then_answer_and_append_only_history(client, monkeypatch):
    fake = FakeClient(
        [
            (
                [
                    say("A missed-call bot fits well."),
                    call("update_lead", {"industry": "dental clinic", "problem": "Front desk misses calls", "budget": "700_2500"}),
                ],
                "completed",
            ),
            ([say("Want me to email you a short brief?", 2)], "completed"),
            ([say("Great, what's your email?", 3)], "completed"),
        ]
    )
    monkeypatch.setattr(agent, "client", lambda: fake)

    res = client.post(
        "/v1/chat",
        headers=HEADERS,
        json={"message": "We're a dental clinic missing calls", "page": "/pricing", "timezone": "Asia/Karachi"},
    )
    events = parse_sse(res.text)
    names = [e[0] for e in events]
    assert names[0] == "session" and names[-1] == "done"
    brief = next(d for n, d in events if n == "brief")
    assert brief["fields"]["budget"] == "$700 to $2,500" and "score" not in json.dumps(brief)
    text = "".join(d["delta"] for n, d in events if n == "text")
    assert text == "A missed-call bot fits well.\n\nWant me to email you a short brief?"

    first, second = fake.requests
    kinds = [i.get("role") or i["type"] for i in second["input"]]
    assert kinds == ["developer", "user", "assistant", "function_call", "function_call_output"]
    assert "Page: /pricing" in second["input"][0]["content"]
    result = outputs(second)[0]
    assert result["call_id"] == "call_1"
    assert json.loads(result["output"])["next_action"] == "offer_brief"
    assert first["store"] is False and first["reasoning"] == {"effort": "low"}
    assert first["include"] == ["reasoning.encrypted_content"]
    assert first["tools"][0]["type"] == "function" and "parameters" in first["tools"][0]

    # Next turn: the stored history is replayed unchanged, with no new
    # context message for a visitor who is still here.
    session_id = events[0][1]["id"]
    client.post("/v1/chat", headers=HEADERS, json={"message": "Sure", "session_id": session_id})
    third = fake.requests[2]
    sent_before = second["input"] + [say("Want me to email you a short brief?", 2).model_dump(mode="json", exclude_none=True)]
    assert third["input"][: len(sent_before)] == sent_before
    assert third["input"][-1] == {"role": "user", "content": "Sure"}
    assert first["instructions"] == third["instructions"] and first["tools"] == third["tools"]

    history = client.get(f"/v1/chat/{session_id}", headers=HEADERS).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant", "user", "assistant"]
    assert history["messages"][1]["content"].startswith("A missed-call bot")
    assert history["brief"]["fields"]["need"] == "Front desk misses calls"


def test_invalid_tool_input_is_returned_as_error(client, monkeypatch):
    fake = FakeClient(
        [
            ([call("update_lead", {"email": "not-an-email"})], "completed"),
            ([say("Could you double-check that email?")], "completed"),
        ]
    )
    monkeypatch.setattr(agent, "client", lambda: fake)
    client.post("/v1/chat", headers=HEADERS, json={"message": "my email is not-an-email"})
    result = outputs(fake.requests[1])[0]
    assert "error" in json.loads(result["output"]) and "email" in result["output"]


def test_booking_without_calendar_shares_link(client, monkeypatch):
    fake = FakeClient(
        [
            ([call("check_availability", {})], "completed"),
            ([say("Here's the link.")], "completed"),
        ]
    )
    monkeypatch.setattr(agent, "client", lambda: fake)
    client.post("/v1/chat", headers=HEADERS, json={"message": "Can we book a call?"})
    result = json.loads(outputs(fake.requests[1])[0]["output"])
    assert result["available"] is False and result["booking_link"]


def test_api_failure_shows_fallback_and_keeps_history_consistent(client, monkeypatch):
    class Broken:
        responses = SimpleNamespace(create=None)

        def __init__(self):
            async def create(**params):
                raise RuntimeError("boom")

            self.responses = SimpleNamespace(create=create)

    monkeypatch.setattr(agent, "client", lambda: Broken())
    events = parse_sse(client.post("/v1/chat", headers=HEADERS, json={"message": "hello"}).text)
    assert any(n == "text" and "couldn't answer" in d["delta"] for n, d in events)
    history = client.get(f"/v1/chat/{events[0][1]['id']}", headers=HEADERS).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]


def test_admin_requires_token(client):
    assert client.get("/v1/admin/leads").status_code == 401
    assert client.get("/v1/admin/leads", headers={"Authorization": "Bearer admin-token"}).status_code == 200


def test_unknown_timezone_defaults_to_new_york(client, monkeypatch):
    fake = FakeClient([([say("Hi!")], "completed")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    client.post("/v1/chat", headers=HEADERS, json={"message": "hello"})
    context = fake.requests[0]["input"][0]["content"]
    assert "(America/New_York, assumed" in context


def test_known_timezone_is_kept(client, monkeypatch):
    fake = FakeClient([([say("Hi!")], "completed")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    client.post("/v1/chat", headers=HEADERS, json={"message": "hello", "timezone": "America/Los_Angeles"})
    context = fake.requests[0]["input"][0]["content"]
    assert "(America/Los_Angeles)" in context


def test_team_hears_about_every_reachable_lead_once(client, monkeypatch):
    from app.config import get_settings
    from app.integrations import mailer

    sent = []
    monkeypatch.setattr(get_settings(), "owner_email", "owner@scalryx.com")
    monkeypatch.setattr(mailer, "send_later", lambda to, subject, text, reply_to=None: sent.append(subject))
    fake = FakeClient(
        [
            ([call("update_lead", {"budget": "under_700", "service_interest": ["automations"]})], "completed"),
            ([say("We can start small.")], "completed"),
            ([call("update_lead", {"phone": "+1 555 0100", "name": "Ali"}, 2)], "completed"),
            ([say("A team member will message you.", 2)], "completed"),
            ([call("update_lead", {"email": "ali@example.com"}, 3)], "completed"),
            ([say("Got it.", 3)], "completed"),
        ]
    )
    monkeypatch.setattr(agent, "client", lambda: fake)

    sid = parse_sse(client.post("/v1/chat", headers=HEADERS, json={"message": "my budget is $100"}).text)[0][1]["id"]
    assert sent == []  # nothing to follow up on yet
    first = json.loads(outputs(fake.requests[1])[0]["output"])
    assert first["next_action"] == "small_budget"

    client.post("/v1/chat", headers=HEADERS, json={"message": "+1 555 0100, I'm Ali", "session_id": sid})
    assert sent == ["New lead to follow up: Ali (cold, 15/100)"]
    assert json.loads(outputs(fake.requests[3])[-1]["output"])["next_action"] == "team_followup"

    client.post("/v1/chat", headers=HEADERS, json={"message": "ali@example.com", "session_id": sid})
    assert len(sent) == 1  # one alert per lead, not one per detail


def test_handoff_in_same_turn_as_lead_alert_is_not_emailed_twice(client, monkeypatch):
    from app.config import get_settings
    from app.integrations import mailer

    sent = []
    monkeypatch.setattr(get_settings(), "owner_email", "owner@scalryx.com")
    monkeypatch.setattr(mailer, "send_later", lambda to, subject, text, reply_to=None: sent.append(subject))
    fake = FakeClient(
        [
            ([call("update_lead", {"phone": "+1 555 0100"}), call("handoff_to_human", {"reason": "wants a follow up"}, 2)], "completed"),
            ([say("The team will message you.")], "completed"),
            ([call("handoff_to_human", {"reason": "complaint", "urgency": "high"}, 3)], "completed"),
            ([say("Flagged as urgent.", 2)], "completed"),
        ]
    )
    monkeypatch.setattr(agent, "client", lambda: fake)
    sid = parse_sse(client.post("/v1/chat", headers=HEADERS, json={"message": "+1 555 0100"}).text)[0][1]["id"]
    assert len(sent) == 1 and sent[0].startswith("New lead to follow up")
    client.post("/v1/chat", headers=HEADERS, json={"message": "this is urgent", "session_id": sid})
    assert len(sent) == 2 and sent[1].startswith("URGENT: Chat handoff")
