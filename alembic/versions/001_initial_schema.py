"""initial schema.

Revision ID: 001
Revises:
Create Date: 2026-04-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "routing_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("recipient_field", sa.Text(), nullable=False),
        sa.Column("recipient_role", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False, server_default=sa.text("'normal'")),
        sa.Column("ignore_quiet_hours", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_routing_rules_unique",
        "routing_rules",
        ["event_type", "recipient_field", "recipient_role"],
        unique=True,
    )

    op.create_table(
        "processed_events",
        sa.Column("cloud_event_id", sa.Text(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("cloud_event_id"),
    )

    op.create_table(
        "notification_outbox",
        sa.Column(
            "id",
            postgresql.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("cloud_event_id", sa.Text(), nullable=False),
        sa.Column("booking_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("recipient_address", sa.Text(), nullable=False),
        sa.Column("recipient_role", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "template_context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "idx_outbox_pending",
        "notification_outbox",
        ["scheduled_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Seed routing rules — recipient_field matches the JSON field name in booking payloads
    # recipient_role uses "organizer" (not "volunteer")
    op.execute(
        sa.text("""
            INSERT INTO routing_rules (event_type, recipient_field, recipient_role) VALUES
                ('booking.created',       'organizer_id', 'organizer'),
                ('booking.created',       'client_id',    'client'),
                ('booking.cancelled',     'organizer_id', 'organizer'),
                ('booking.cancelled',     'client_id',    'client'),
                ('booking.rescheduled',   'organizer_id', 'organizer'),
                ('booking.rescheduled',   'client_id',    'client'),
                ('booking.reassigned',    'organizer_id', 'organizer'),
                ('booking.reassigned',    'client_id',    'client'),
                ('booking.reminder_sent', 'client_id',    'client')
            ON CONFLICT DO NOTHING
        """)
    )


def downgrade() -> None:
    op.drop_index("idx_outbox_pending", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_table("processed_events")
    op.drop_index("idx_routing_rules_unique", table_name="routing_rules")
    op.drop_table("routing_rules")
