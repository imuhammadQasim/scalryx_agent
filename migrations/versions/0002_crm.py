"""CRM: leads, activities, admin users and sessions.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("business_name", sa.String(200), nullable=False),
        sa.Column("contact_name", sa.String(120)),
        sa.Column("phone", sa.String(40)),
        sa.Column("phone_key", sa.String(20)),
        sa.Column("email", sa.String(254)),
        sa.Column("website", sa.String(300)),
        sa.Column("industry", sa.String(120)),
        sa.Column("city", sa.String(100)),
        sa.Column("country", sa.String(100)),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("is_hot", sa.Boolean, nullable=False),
        sa.Column("notes", sa.Text, nullable=False),
        sa.Column("next_follow_up_at", TS),
        sa.Column("lost_reason", sa.String(300)),
        sa.Column("external_ref", sa.String(64)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    for col in ("phone_key", "email", "source", "status", "is_hot", "next_follow_up_at", "external_ref"):
        op.create_index(f"ix_leads_{col}", "leads", [col])

    op.create_table(
        "activities",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer, sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_activities_lead_id", "activities", ["lead_id"])

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
    )
    op.create_index("ix_admin_sessions_user_id", "admin_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("admin_sessions")
    op.drop_table("admin_users")
    op.drop_table("activities")
    op.drop_table("leads")
