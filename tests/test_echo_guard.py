"""Tool results the model echoes as text never reach the visitor."""

import json

from app import agent
from app.agent import EchoGuard, strip_json_echo
from tests.test_chat import HEADERS, FakeClient, call, client, parse_sse, say  # noqa: F401


def run(chunks):
    guard = EchoGuard()
    return "".join(guard.feed(c) for c in chunks) + guard.flush()


def test_echoed_json_split_across_chunks_is_dropped():
    chunks = ['{"saved": ["current', '_cost"], "next_action": "keep', '_helping"}', "That would save ", "9 hours."]
    assert run(chunks) == "That would save 9 hours."


def test_two_echoed_objects_are_dropped():
    assert run(['{"a": 1}', ' {"b": {"c": 2}}', "Hi"]) == "Hi"


def test_plain_text_streams_through_untouched():
    guard = EchoGuard()
    assert guard.feed("Hello") == "Hello"
    assert guard.feed(" {not json} there") == " {not json} there"


def test_brace_text_that_is_not_json_is_released():
    assert run(["{curly", " braces} are fine"]) == "{curly braces} are fine"


def test_strip_json_echo():
    assert strip_json_echo('{"x": 1}\nThanks!') == "Thanks!"
    assert strip_json_echo("No JSON here {x}") == "No JSON here {x}"


def test_echo_is_removed_from_stream_and_storage(client, monkeypatch):  # noqa: F811
    fake = FakeClient(
        [
            ([call("update_lead", {"industry": "clinic"})], "completed"),
            ([say('{"saved": ["industry"], "next_action": "keep_helping"}Got it, a clinic.')], "completed"),
            ([say("Sure.", 2)], "completed"),
        ]
    )
    monkeypatch.setattr(agent, "client", lambda: fake)
    events = parse_sse(client.post("/v1/chat", headers=HEADERS, json={"message": "I run a clinic"}).text)
    assert "".join(d["delta"] for n, d in events if n == "text") == "Got it, a clinic."

    session_id = events[0][1]["id"]
    client.post("/v1/chat", headers=HEADERS, json={"message": "ok", "session_id": session_id})
    replayed = [i for i in fake.requests[2]["input"] if i.get("type") == "message"]
    assert replayed[-1]["content"][0]["text"] == "Got it, a clinic."
    assert "saved" not in json.dumps(client.get(f"/v1/chat/{session_id}", headers=HEADERS).json()["messages"])
