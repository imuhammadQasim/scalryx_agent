"""Lead scoring in plain code, so a score can always be explained.

The model gathers facts with `update_lead`; this module turns them into a
score, a tier and the next move, and the model follows that move.
"""

from dataclasses import dataclass, field
from typing import Any

BUDGETS = {
    "under_700": ("Under $700", 5),
    "700_2500": ("$700 to $2,500", 22),
    "2500_8000": ("$2,500 to $8,000", 30),
    "8000_plus": ("$8,000+", 30),
    "unsure": ("Not sure yet", 10),
}

TIMELINES = {
    "asap": ("As soon as possible", 20),
    "1_3_months": ("In 1 to 3 months", 15),
    "3_plus_months": ("In 3+ months", 6),
    "exploring": ("Just exploring", 2),
}

HOT = 70
WARM = 40

# What the agent should try to learn next, in the order it usually flows.
DISCOVERY_ORDER = ["problem", "business", "timeline", "budget", "name", "email", "decision_maker"]


@dataclass
class Assessment:
    score: int
    tier: str
    next_action: str
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _known(profile: dict[str, Any], key: str) -> bool:
    if key == "business":
        return bool(profile.get("company") or profile.get("industry"))
    value = profile.get(key)
    return value is not None and value != "" and value != []


def assess(profile: dict[str, Any], booked: bool = False) -> Assessment:
    score = 0
    reasons: list[str] = []

    def add(points: int, why: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"+{points} {why}")

    if profile.get("problem"):
        add(15, "clear problem")
    if profile.get("service_interest"):
        add(5, "knows what they want")
    if _known(profile, "business"):
        add(5, "business context")
    if budget := BUDGETS.get(profile.get("budget") or ""):
        add(budget[1], f"budget {budget[0]}")
    if timeline := TIMELINES.get(profile.get("timeline") or ""):
        add(timeline[1], f"timeline {timeline[0].lower()}")
    if profile.get("decision_maker") is True:
        add(10, "decision maker")
    if profile.get("email"):
        add(15, "email")
    if profile.get("phone"):
        add(5, "phone")

    score = min(score, 100)
    tier = "hot" if score >= HOT else "warm" if score >= WARM else "cold"
    missing = [key for key in DISCOVERY_ORDER if not _known(profile, key)]
    has_contact = bool(profile.get("name") and profile.get("email"))
    reachable = bool(profile.get("email") or profile.get("phone"))

    if booked:
        action = "booked"
    elif profile.get("budget") == "under_700":
        # Below the usual starting price is still a lead: get a way to reach
        # them, and let a person work out what is possible for that budget.
        action = "team_followup" if reachable else "small_budget"
    elif tier == "hot":
        action = "offer_call" if has_contact else "ask_contact_for_call"
    elif tier == "warm":
        action = "offer_brief" if not profile.get("email") else "offer_call"
    else:
        action = "keep_helping"

    return Assessment(score=score, tier=tier, next_action=action, missing=missing, reasons=reasons)


def brief_card(profile: dict[str, Any]) -> dict[str, Any]:
    """The visitor-facing summary shown in the chat. No score or tier: the
    visitor sees what we understood, never how we rated them."""
    budget = BUDGETS.get(profile.get("budget") or "")
    timeline = TIMELINES.get(profile.get("timeline") or "")
    business = " · ".join(v for v in (profile.get("company"), profile.get("industry")) if v)
    fields = {
        "business": business or None,
        "need": profile.get("problem"),
        "goal": profile.get("goal"),
        "budget": budget[0] if budget else None,
        "timeline": timeline[0] if timeline else None,
    }
    return {"fields": fields, "filled": sum(1 for v in fields.values() if v), "total": len(fields)}
