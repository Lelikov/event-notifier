"""PostgreSQL schema bootstrap for event-notifier."""

import asyncpg

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routing_rules (
    id          SERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,
    recipient_field TEXT NOT NULL,
    recipient_role  TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'normal',
    ignore_quiet_hours BOOLEAN NOT NULL DEFAULT FALSE,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_rules_unique
    ON routing_rules (event_type, recipient_field, recipient_role);

CREATE TABLE IF NOT EXISTS processed_events (
    cloud_event_id TEXT PRIMARY KEY,
    processed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key   TEXT NOT NULL UNIQUE,
    cloud_event_id    TEXT NOT NULL,
    booking_id        TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    recipient_address TEXT NOT NULL,
    recipient_role    TEXT NOT NULL,
    channel           TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    template_context  JSONB NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'delivered', 'failed')),
    retry_count       INT NOT NULL DEFAULT 0,
    max_retries       INT NOT NULL DEFAULT 5,
    scheduled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON notification_outbox (scheduled_at)
    WHERE status = 'pending';
"""

_SEED_SQL = """
INSERT INTO routing_rules (event_type, recipient_field, recipient_role)
VALUES
    ('booking.created',      'volunteer_id', 'volunteer'),
    ('booking.created',      'client_id',    'client'),
    ('booking.cancelled',    'volunteer_id', 'volunteer'),
    ('booking.cancelled',    'client_id',    'client'),
    ('booking.rescheduled',  'volunteer_id', 'volunteer'),
    ('booking.rescheduled',  'client_id',    'client'),
    ('booking.reassigned',   'volunteer_id', 'volunteer'),
    ('booking.reassigned',   'client_id',    'client'),
    ('booking.reminder_sent','client_id',    'client')
ON CONFLICT DO NOTHING;
"""


async def create_tables(pool: asyncpg.Pool) -> None:
    """Create all tables and seed routing rules. Idempotent."""
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        await conn.execute(_SEED_SQL)
