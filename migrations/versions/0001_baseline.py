"""Baseline: the chatbot's tables, with `leads` renamed to `chat_leads`.

Databases created before Alembic have conversations, messages, leads and
bookings. For those, only the rename is applied. Fresh databases get all four.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _create_chat_leads() -> None:
    op.create_table(
        "chat_leads",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id"), nullable=False, unique=True),
        sa.Column("email", sa.String(254)),
        sa.Column("profile", sa.JSON, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("tier", sa.String(8), nullable=False),
        sa.Column("booked", sa.Boolean, nullable=False),
        sa.Column("hot_alert_sent_at", sa.DateTime(timezone=True)),
        sa.Column("brief_sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_leads_email", "chat_leads", ["email"])


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "leads" in tables and "chat_leads" not in tables:
        # Legacy database: keep every row, just give the table its new name.
        op.drop_index("ix_leads_email", table_name="leads")
        op.rename_table("leads", "chat_leads")
        op.create_index("ix_chat_leads_email", "chat_leads", ["email"])
        return

    if "conversations" in tables:
        return

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(64)),
        sa.Column("timezone", sa.String(64)),
        sa.Column("source", sa.JSON, nullable=False),
        sa.Column("user_turns", sa.Integer, nullable=False),
        sa.Column("state", sa.JSON, nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("item", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    _create_chat_leads()
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("cal_uid", sa.String(128)),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True)),
        sa.Column("location", sa.String(500)),
        sa.Column("attendee_name", sa.String(120), nullable=False),
        sa.Column("attendee_email", sa.String(254), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bookings_conversation_id", "bookings", ["conversation_id"])


def downgrade() -> None:
    raise NotImplementedError("The baseline cannot be downgraded.")
