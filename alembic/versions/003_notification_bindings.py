"""notification_bindings: admin-managed per-event channel + template config."""

import json
import os
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

_TRIGGERS = [
    "BOOKING_CREATED",
    "BOOKING_RESCHEDULED",
    "BOOKING_REASSIGNED",
    "BOOKING_CANCELLED",
    "BOOKING_REMINDER",
    "BOOKING_REJECTED",
    "BOOKING_REJECTED_BLACKLISTED",
]


def _seed_rows() -> list[dict]:
    default_locale = os.getenv("DEFAULT_LOCALE", "ru")
    raw = os.getenv("UNISENDER_TEMPLATE_IDS", "{}")
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    # Flatten to {TRIGGER: uuid} for the default locale (mirrors unisender_template_ids_by_locale).
    email_ids: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(value, dict):
            if key == default_locale:
                email_ids.update(value)
        elif isinstance(value, str):
            email_ids[key] = value

    tg_dir = (
        Path(__file__).resolve().parents[2] / "event_notifier" / "templates" / default_locale / "telegram"
    )
    rows = []
    for trigger in _TRIGGERS:
        rows.append(
            {
                "trigger_event": trigger,
                "channel": "email",
                "enabled": True,
                "unisender_template_id": email_ids.get(trigger),
                "telegram_body": None,
            }
        )
        tg_file = tg_dir / f"{trigger}.j2"
        body = tg_file.read_text(encoding="utf-8") if tg_file.exists() else None
        rows.append(
            {
                "trigger_event": trigger,
                "channel": "telegram",
                "enabled": body is not None,
                "unisender_template_id": None,
                "telegram_body": body,
            }
        )
    return rows


def upgrade() -> None:
    table = op.create_table(
        "notification_bindings",
        sa.Column("trigger_event", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("unisender_template_id", sa.Text(), nullable=True),
        sa.Column("telegram_body", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("trigger_event", "channel", name="pk_notification_bindings"),
    )
    op.bulk_insert(table, _seed_rows())


def downgrade() -> None:
    op.drop_table("notification_bindings")
