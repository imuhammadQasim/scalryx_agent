"""Business facts exported from the website (scripts/export-knowledge.mjs)."""

import json
from functools import lru_cache
from typing import Any

from .config import get_settings


@lru_cache
def load() -> dict[str, Any]:
    return json.loads(get_settings().knowledge_path.read_text(encoding="utf-8"))


def service_ids() -> list[str]:
    return [s["id"] for s in load()["services"]]


def tier_ids() -> list[str]:
    return [t["id"] for t in load()["pricingTiers"]]


def tier(tier_id: str) -> dict[str, Any] | None:
    return next((t for t in load()["pricingTiers"] if t["id"] == tier_id), None)


def price_line(t: dict[str, Any]) -> str:
    p = t["price"]
    return f"{p['prefix']} {p['amount']}, {p['unit']}"


def as_text() -> str:
    """The facts as compact plain text for the system prompt."""
    k = load()
    site, cap = k["site"], k["capacity"]
    out = [
        f"Company: {site['description']}",
        f"Website: {site['url']}. Email: {site['email']}. WhatsApp: {site['whatsapp']}. {site['responseTime']}.",
        f"Hire through Upwork ({site['upwork']}) or Fiverr ({site['fiverr']}); payment sits in escrow until the client approves each stage.",
        f"Capacity: {cap['open']} of {cap['perMonth']} project spots are open this month.",
        "",
        "Services:",
        *(f"- {s['name']} [{s['id']}]: {s['description']} {'; '.join(s['points'])}. {s['outcome']}." for s in k["services"]),
        "",
        "Packages (all prices are starting prices; the exact fixed price comes in writing after the call):",
    ]
    for t in k["pricingTiers"]:
        out.append(
            f"- {t['name']} [{t['id']}], for {t['audience'].lower()}: {price_line(t)}. {t['promise']}. "
            f"Includes: {'; '.join(t['features'])}. Bonus: {t['bonus']['label']} (worth {t['bonus']['value']}). {t['note']}."
        )
    out += [
        "",
        "Monthly care plans after launch:",
        *(f"- {p['name']} ({p['price']['amount']} {p['price']['unit']}): {p['promise']}" for p in k["supportPlans"]),
        "",
        "Every package includes: " + "; ".join(k["includes"]) + ".",
        "Guarantees: " + " ".join(f"{g['title']}: {g['text']}" for g in k["guarantees"]),
        "",
        "How a project works:",
        *(f"{i}. {p['title']} ({p['meta']}): {p['text']}" for i, p in enumerate(k["process"], 1)),
        "",
        "Who we help:",
        *(f"- {a['label']}: {a['pain']} We build: {'; '.join(a['build'])}. Result: {a['result']} Usual package: {a['plan']}." for a in k["audiences"]),
        "",
        "FAQ:",
        *(f"Q: {f['q']}\nA: {f['a']}" for f in k["faqs"]),
    ]
    return "\n".join(out)
