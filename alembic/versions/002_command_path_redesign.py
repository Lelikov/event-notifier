"""Command-path redesign: trigger_event/recipient_email/last_error columns,
status CHECK + processing index, drop dead routing_rules and event_type.

Revision ID: 002
Revises: 001
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002"
down_revision: str | Sequence[str] | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # trigger_event replaces event_type: the only consumed event is
    # notification.send_requested, and the template selector is the payload's
    # trigger_event (BOOKING_CREATED, ...), which previously was never stored.
    op.add_column(
        "notification_outbox",
        sa.Column("trigger_event", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.execute("UPDATE notification_outbox SET trigger_event = '' WHERE trigger_event IS NULL")
    op.alter_column("notification_outbox", "trigger_event", server_default=None)

    # recipient_email: needed by delivery-result events for non-email channels.
    op.add_column(
        "notification_outbox",
        sa.Column("recipient_email", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.execute("UPDATE notification_outbox SET recipient_email = recipient_address WHERE channel = 'email'")
    op.alter_column("notification_outbox", "recipient_email", server_default=None)

    op.add_column("notification_outbox", sa.Column("last_error", sa.Text(), nullable=True))

    op.drop_column("notification_outbox", "event_type")

    # Extended retry budget (capped exponential backoff in OutboxSender).
    op.alter_column("notification_outbox", "max_retries", server_default=sa.text("10"))
    op.execute("UPDATE notification_outbox SET max_retries = 10 WHERE max_retries = 5")

    op.create_check_constraint(
        "ck_outbox_status",
        "notification_outbox",
        "status IN ('pending', 'processing', 'delivered', 'failed')",
    )
    op.create_index(
        "idx_outbox_processing",
        "notification_outbox",
        ["updated_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )

    # The routing-rules machinery was unreachable dead code: only
    # notification.send_requested is routed to this service's queue.
    op.drop_index("idx_routing_rules_unique", table_name="routing_rules")
    op.drop_table("routing_rules")


def downgrade() -> None:
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
    op.drop_index("idx_outbox_processing", table_name="notification_outbox")
    op.drop_constraint("ck_outbox_status", "notification_outbox", type_="check")
    op.alter_column("notification_outbox", "max_retries", server_default=sa.text("5"))
    op.add_column(
        "notification_outbox",
        sa.Column("event_type", sa.Text(), nullable=False, server_default=sa.text("'notification.send_requested'")),
    )
    op.alter_column("notification_outbox", "event_type", server_default=None)
    op.drop_column("notification_outbox", "last_error")
    op.drop_column("notification_outbox", "recipient_email")
    op.drop_column("notification_outbox", "trigger_event")
