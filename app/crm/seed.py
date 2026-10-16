"""Create or reset the admin login, and optionally add sample leads.

    python -m app.crm.seed              # admin from ADMIN_EMAIL / ADMIN_PASSWORD
    python -m app.crm.seed --sample     # also add demo leads (only into an empty table)

Run it again after changing ADMIN_PASSWORD to reset the password; all
existing sessions are signed out.
"""

import argparse
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import delete, func, select

from ..config import get_settings
from ..db import SessionLocal, init_db, utcnow
from . import service
from .models import AdminSession, AdminUser, Lead
from .schemas import LeadCreate
from .security import find_admin, hash_password

MIN_PASSWORD = 12


def sample_leads() -> list[LeadCreate]:
    now = utcnow()
    rows = [
        ("Bright Smile Dental", "Dr. Amna Rauf", "+1 415 555 0134", "amna@brightsmile.example", "interested", True, "cold_call", 0, "Wants online booking; asked for a quote."),
        ("Northside Plumbing LLC", "Mike Dunn", "+1 646 555 0188", None, "contacted", False, "cold_call", -2, "Left voicemail. Call back after 2pm."),
        ("Crescent Bakery", "Sana Ali", "+44 20 7946 0958", "sana@crescent.example", "new", False, "chatbot", 1, "[chatbot] Asked about a Shopify store for online orders."),
        ("Atlas Legal Group", "Henry Cole", "+1 312 555 0102", "hcole@atlas.example", "proposal_sent", True, "referral", 3, "Proposal sent for website + intake bot."),
        ("Pixel & Pine Studio", "Lena Ortiz", "+1 503 555 0170", "lena@pixelpine.example", "won", False, "website_form", None, "Signed the starter package."),
        ("Greenfield Auto Repair", "Tom Reyes", "+1 214 555 0119", None, "lost", False, "cold_call", None, "Went with a cousin's web designer."),
        ("Harbor Yoga", "Priya Nair", "+1 206 555 0145", "priya@harboryoga.example", "interested", False, "cold_call", -1, "Needs class booking. Follow up Friday."),
    ]  # fmt: skip
    leads = []
    for business, contact, phone, email, status, hot, source, days, notes in rows:
        leads.append(
            LeadCreate(
                business_name=business,
                contact_name=contact,
                phone=phone,
                email=email,
                status=status,
                is_hot=hot,
                source=source,
                notes=notes,
                next_follow_up_at=None if days is None else now.replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=days),
                lost_reason="Chose another provider" if status == "lost" else None,
            )
        )
    return leads


async def run(sample: bool) -> int:
    s = get_settings()
    email = s.admin_email.strip().lower()
    if not email or not s.admin_password:
        print("Set ADMIN_EMAIL and ADMIN_PASSWORD (in .env or the environment) first.", file=sys.stderr)
        return 1
    if len(s.admin_password) < MIN_PASSWORD:
        print(f"ADMIN_PASSWORD must be at least {MIN_PASSWORD} characters.", file=sys.stderr)
        return 1

    await init_db()
    async with SessionLocal() as db:
        user = await find_admin(db, email)
        if user is None:
            db.add(AdminUser(email=email, password_hash=hash_password(s.admin_password)))
            print(f"Created admin {email}")
        else:
            user.password_hash = hash_password(s.admin_password)
            await db.execute(delete(AdminSession).where(AdminSession.user_id == user.id))
            print(f"Reset password for {email} (sessions signed out)")

        if sample:
            if await db.scalar(select(func.count(Lead.id))):
                print("Leads already exist; skipped sample data.")
            else:
                for data in sample_leads():
                    await service.create_lead(db, data)
                print("Added sample leads.")
        await db.commit()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", action="store_true", help="add demo leads if there are none yet")
    sys.exit(asyncio.run(run(parser.parse_args().sample)))


if __name__ == "__main__":
    main()
