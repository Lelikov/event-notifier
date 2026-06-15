"""notification_bindings: add recipient_role, expand rows per role, re-key PK."""

import sqlalchemy as sa

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows become the 'client' rows (server_default backfills them).
    op.add_column(
        "notification_bindings",
        sa.Column("recipient_role", sa.Text(), nullable=False, server_default="client"),
    )
    # Drop the old PK (trigger_event, channel) before inserting cloned rows,
    # because the old PK would reject duplicate (trigger_event, channel) pairs.
    op.drop_constraint("pk_notification_bindings", "notification_bindings", type_="primary")
    # Clone every existing row for the organizer with identical values.
    op.execute(
        """
        INSERT INTO notification_bindings
            (trigger_event, recipient_role, channel, enabled, unisender_template_id, telegram_body, updated_at)
        SELECT trigger_event, 'organizer', channel, enabled, unisender_template_id, telegram_body, now()
        FROM notification_bindings
        WHERE recipient_role = 'client'
        """
    )
    op.create_primary_key(
        "pk_notification_bindings",
        "notification_bindings",
        ["trigger_event", "recipient_role", "channel"],
    )
    # Writes are explicit thereafter; drop the backfill default.
    op.alter_column("notification_bindings", "recipient_role", server_default=None)


def downgrade() -> None:
    op.execute("DELETE FROM notification_bindings WHERE recipient_role = 'organizer'")
    op.drop_constraint("pk_notification_bindings", "notification_bindings", type_="primary")
    op.create_primary_key(
        "pk_notification_bindings",
        "notification_bindings",
        ["trigger_event", "channel"],
    )
    op.drop_column("notification_bindings", "recipient_role")
