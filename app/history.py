"""Turning stored Responses API items into what the visitor (or the team) reads."""

from typing import Any


def _assistant_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content.strip()
    parts = content or []
    text = "".join(p.get("text") or p.get("refusal") or "" for p in parts if p.get("type") in ("output_text", "refusal"))
    # An all-JSON message is stored as is (see agent._clean); never show it.
    return "" if text.lstrip().startswith("{") else text.strip()


def display_messages(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Visitor and assistant text only. Tool calls, reasoning and context are
    hidden, and the assistant messages of one turn merge into one bubble."""
    out: list[dict[str, str]] = []
    for item in items:
        role = item.get("role")
        if role == "user" and isinstance(item.get("content"), str):
            out.append({"role": "user", "content": item["content"]})
        elif role == "assistant":
            text = _assistant_text(item)
            if not text:
                continue
            if out and out[-1]["role"] == "assistant":
                out[-1]["content"] += "\n\n" + text
            else:
                out.append({"role": "assistant", "content": text})
    return out


def transcript(items: list[dict[str, Any]], limit: int = 40) -> str:
    lines = [f"{'Visitor' if m['role'] == 'user' else 'Scout'}: {m['content']}" for m in display_messages(items)]
    return "\n\n".join(lines[-limit:])
