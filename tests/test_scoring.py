from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.scoring import assess, brief_card
from app.tools import pick_slots, slot_label


def test_empty_profile_is_cold_and_keeps_helping():
    a = assess({})
    assert (a.score, a.tier, a.next_action) == (0, "cold", "keep_helping")
    assert a.missing[0] == "problem"


def test_warm_lead_without_email_gets_offered_a_brief():
    a = assess({"problem": "Missed calls", "industry": "dental", "budget": "700_2500"})
    assert a.tier == "warm"
    assert a.next_action == "offer_brief"


def test_hot_lead_without_contact_is_asked_for_it():
    a = assess({"problem": "x", "company": "Acme", "budget": "2500_8000", "timeline": "asap"})
    assert a.tier == "hot"
    assert a.next_action == "ask_contact_for_call"


def test_hot_lead_with_contact_is_offered_the_call():
    a = assess({"problem": "x", "budget": "8000_plus", "timeline": "1_3_months", "name": "Sara", "email": "s@x.com"})
    assert a.next_action == "offer_call"
    assert a.score <= 100


def test_small_budget_is_kept_as_a_lead():
    # Never turned away: ask for a way to reach them, then hand to the team.
    assert assess({"service_interest": ["automations"], "budget": "under_700"}).next_action == "small_budget"
    assert assess({"budget": "under_700", "phone": "+1 555 0100"}).next_action == "team_followup"
    assert assess({"budget": "under_700", "email": "a@b.co"}).next_action == "team_followup"


def test_booked_overrides_everything():
    assert assess({"email": "a@b.co"}, booked=True).next_action == "booked"


def test_brief_card_hides_score():
    card = brief_card({"company": "Acme", "industry": "retail", "budget": "700_2500"})
    assert card["fields"]["business"] == "Acme · retail"
    assert card["fields"]["budget"] == "$700 to $2,500"
    assert card["filled"] == 2 and "score" not in card


def test_pick_slots_spreads_two_per_day():
    base = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)  # 9:00 in Karachi
    slots = [base + timedelta(minutes=30 * i) for i in range(16)] + [base + timedelta(days=1)]
    picked = pick_slots(slots, "Asia/Karachi", "any")
    assert len(picked) == 3
    assert picked[1] - picked[0] >= timedelta(hours=3)
    assert slot_label(picked[0], "Asia/Karachi") == "Fri, Oct 16, 9:00 AM"


def test_pick_slots_filters_part_of_day():
    base = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)
    slots = [base + timedelta(hours=i) for i in range(10)]
    picked = pick_slots(slots, "Asia/Karachi", "afternoon")
    assert picked and all(12 <= p.astimezone(ZoneInfo("Asia/Karachi")).hour < 17 for p in picked)


def test_round_the_clock_calendar_only_offers_waking_hours():
    # Cal.com is open 24/7; a New York visitor should never be offered 2 AM.
    start = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)  # midnight in New York
    slots = [start + timedelta(minutes=30 * i) for i in range(48 * 2)]
    picked = pick_slots(slots, "America/New_York", "any")
    hours = [p.astimezone(ZoneInfo("America/New_York")).hour for p in picked]
    assert picked and all(8 <= h < 21 for h in hours)
    assert hours[0] == 8 and slot_label(picked[0], "America/New_York") == "Fri, Oct 16, 8:00 AM"
