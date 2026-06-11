# Alembic + DeclarativeBase + event-schemas Integration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace asyncpg raw SQL with SQLAlchemy DeclarativeBase ORM models + Alembic migrations, and integrate event-schemas enums (`TriggerEvent`, `RecipientRole`) throughout the codebase, replacing hardcoded `"volunteer"` with `RecipientRole.ORGANIZER`.

**Architecture:** ORM models exist only for Alembic schema management — all queries go through `SqlExecutor` using `text()` SQL (same pattern as event-saver). asyncpg is replaced by SQLAlchemy's `AsyncSession`. Template maps use `TriggerEvent` enum keys instead of raw strings. `RecipientRole` from event-schemas replaces all `"volunteer"` / `"client"` strings.

**Tech Stack:** SQLAlchemy 2.0+ (DeclarativeBase, AsyncSession, mapped_column), Alembic (async migrations), event-schemas (TriggerEvent, RecipientRole), Dishka DI

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `event_notifier/db/base.py` | `DeclarativeBase` base class |
| Create | `event_notifier/db/models.py` | ORM models: `RoutingRuleModel`, `ProcessedEventModel`, `NotificationOutboxModel` |
| Create | `event_notifier/adapters/sql.py` | `SqlExecutor` wrapper over `AsyncSession` |
| Create | `event_notifier/interfaces/sql.py` | `ISqlExecutor` protocol |
| Create | `alembic.ini` | Alembic config |
| Create | `alembic/env.py` | Async migration runner |
| Create | `alembic/script.py.mako` | Migration template |
| Create | `alembic/versions/001_initial_schema.py` | Initial migration (3 tables + seed data) |
| Modify | `event_notifier/db/repository.py` | asyncpg → SqlExecutor |
| Modify | `event_notifier/interfaces/repository.py` | Remove unused import if needed |
| Modify | `event_notifier/ioc.py` | asyncpg pool → AsyncEngine + sessionmaker + SqlExecutor |
| Modify | `event_notifier/main.py` | Remove `create_tables()` call, remove asyncpg import |
| Modify | `event_notifier/config.py` | Ensure `database_url` works with `postgresql+asyncpg://` |
| Modify | `event_notifier/infrastructure/channels/email.py` | `_TEMPLATE_MAP` keys → `TriggerEvent` |
| Modify | `event_notifier/infrastructure/channels/telegram.py` | `_MESSAGE_TEMPLATES` keys → `TriggerEvent` |
| Modify | `event_notifier/infrastructure/channels/push.py` | `_PUSH_TITLES` keys → `TriggerEvent` |
| Modify | `event_notifier/interfaces/channels.py` | `trigger_event: str` → `trigger_event: TriggerEvent` |
| Modify | `event_notifier/domain/models/notification.py` | `RoutingRule.recipient_role` / `ChannelContact.role` / `OutboxRecord.recipient_role` → `RecipientRole` |
| Modify | `event_notifier/event_types.py` | Use `TriggerEvent` values in `DOMAIN_EVENT_TO_TRIGGER` (already does, but type annotation) |
| Modify | `event_notifier/domain/services/routing.py` | Return `RecipientRole` instead of `str` |
| Modify | `event_notifier/application/use_cases/process_domain_event.py` | Use `RecipientRole` for role values |
| Modify | `event_notifier/adapters/outbox_sender.py` | Use `TriggerEvent` for trigger_event |
| Modify | `event_notifier/adapters/consumer.py` | Use `TriggerEvent` for trigger_event |
| Modify | `event_notifier/infrastructure/users_client.py` | Use `RecipientRole` for role param |
| Modify | `event_notifier/interfaces/users_client.py` | Update type hints |
| Delete | `event_notifier/db/schema.py` | No longer needed (replaced by Alembic migrations) |
| Modify | `pyproject.toml` | Replace `asyncpg` with `sqlalchemy[asyncio]`, add `alembic` |
| Modify | `tests/domain/test_routing_service.py` | Update `"volunteer"` → `RecipientRole.ORGANIZER` |
| Modify | `tests/application/test_process_domain_event.py` | Update roles and any string references |
| Modify | `tests/infrastructure/test_outbox_sender.py` | Update `"volunteer"` → `RecipientRole.ORGANIZER` |
| Modify | `tests/infrastructure/test_users_client.py` | Update `"volunteer"` → `RecipientRole.ORGANIZER` |

---

### Task 1: Add SQLAlchemy + Alembic dependencies

**Files:**
- Modify: `pyproject.toml:6-17`

- [ ] **Step 1: Update pyproject.toml dependencies**

Replace `asyncpg` with SQLAlchemy async and add Alembic:

```toml
dependencies = [
    "alembic>=1.15.0",
    "asyncpg>=0.30.0",
    "cloudevents>=1.12.0",
    "dishka>=1.8.0",
    "event-schemas @ git+https://github.com/Lelikov/event-schemas.git",
    "fastapi>=0.135.1",
    "faststream[rabbit]>=0.6.7",
    "httpx>=0.28.0",
    "pydantic-settings>=2.13.1",
    "sqlalchemy[asyncio]>=2.0.0",
    "structlog>=25.5.0",
    "uvicorn>=0.41.0",
]
```

Note: `asyncpg` stays as the async driver for SQLAlchemy (`postgresql+asyncpg://`).

- [ ] **Step 2: Install dependencies**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv sync`
Expected: Dependencies resolve and install successfully.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat(event-notifier): add sqlalchemy and alembic dependencies"
```

---

### Task 2: Create DeclarativeBase and ORM models

**Files:**
- Create: `event_notifier/db/base.py`
- Create: `event_notifier/db/models.py`

- [ ] **Step 1: Create DeclarativeBase**

Create `event_notifier/db/base.py`:

```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

- [ ] **Step 2: Create ORM models**

Create `event_notifier/db/models.py`:

```python
"""ORM models for Alembic schema management. Not used for queries."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from event_notifier.db.base import Base


class RoutingRuleModel(Base):
    __tablename__ = "routing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_field: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_role: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'normal'"))
    ignore_quiet_hours: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        Index("idx_routing_rules_unique", "event_type", "recipient_field", "recipient_role", unique=True),
    )


class ProcessedEventModel(Base):
    __tablename__ = "processed_events"

    cloud_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )


class NotificationOutboxModel(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    cloud_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    booking_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_address: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_role: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    template_context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )

    __table_args__ = (
        Index("idx_outbox_pending", "scheduled_at", postgresql_where=text("status = 'pending'")),
    )
```

- [ ] **Step 3: Verify models import without errors**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run python -c "from event_notifier.db.models import RoutingRuleModel, ProcessedEventModel, NotificationOutboxModel; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add event_notifier/db/base.py event_notifier/db/models.py
git commit -m "feat(event-notifier): add DeclarativeBase and ORM models for Alembic"
```

---

### Task 3: Set up Alembic

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/` (empty directory, migration added in Task 4)

- [ ] **Step 1: Create alembic.ini**

Create `alembic.ini` in the event-notifier root:

```ini
[alembic]
script_location = %(here)s/alembic
prepend_sys_path = .
path_separator = os
sqlalchemy.url = postgresql+asyncpg://user:pass@localhost/dbname

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Create alembic/env.py**

```python
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from event_notifier.config import Settings
from event_notifier.db import models  # noqa: F401
from event_notifier.db.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", str(Settings().database_url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3: Create alembic/script.py.mako**

```mako
"""${message}.

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

import sqlalchemy as sa
${imports if imports else ""}
from alembic import op

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Create empty versions directory**

Run: `mkdir -p /Users/alexandrlelikov/PycharmProjects/events/event-notifier/alembic/versions`

- [ ] **Step 5: Commit**

```bash
git add alembic.ini alembic/
git commit -m "feat(event-notifier): set up Alembic with async migration runner"
```

---

### Task 4: Create initial migration

**Files:**
- Create: `alembic/versions/001_initial_schema.py`

- [ ] **Step 1: Create the initial migration**

Create `alembic/versions/001_initial_schema.py`:

```python
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

    # Seed routing rules — note: "volunteer" is now "organizer"
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
```

Note: `volunteer_id` → `organizer_id` and `"volunteer"` → `"organizer"` in seed data. The booking payloads already use `volunteer_id` field name in data — the `recipient_field` in routing rules must match the field name in event data. **Check with the user**: if the booking payload sends `volunteer_id`, keep `volunteer_id` as `recipient_field` but set `recipient_role` to `"organizer"`. Update the seed accordingly:

Actually, `recipient_field` is the JSON path into the event data (e.g., `data.volunteer_id`). The field name in the booking payload is determined by the sender (event-receiver / booking service). If they send `volunteer_id`, we must use `volunteer_id` as the `recipient_field`. Only `recipient_role` changes to `"organizer"`.

Corrected seed data in the migration should use `volunteer_id` for `recipient_field` but `organizer` for `recipient_role`.

- [ ] **Step 2: Commit**

```bash
git add alembic/versions/001_initial_schema.py
git commit -m "feat(event-notifier): add initial Alembic migration with seed data"
```

---

### Task 5: Create SqlExecutor and ISqlExecutor protocol

**Files:**
- Create: `event_notifier/adapters/sql.py`
- Create: `event_notifier/interfaces/sql.py`

- [ ] **Step 1: Create ISqlExecutor protocol**

Create `event_notifier/interfaces/sql.py`:

```python
from __future__ import annotations
from typing import TYPE_CHECKING, Protocol


if TYPE_CHECKING:
    from sqlalchemy.engine import RowMapping


class ISqlExecutor(Protocol):
    async def fetch_one(self, query: str, values: dict) -> RowMapping | None: ...

    async def fetch_all(self, query: str, values: dict) -> list[RowMapping]: ...

    async def execute(self, query: str, values: dict) -> None: ...
```

- [ ] **Step 2: Create SqlExecutor implementation**

Create `event_notifier/adapters/sql.py`:

```python
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession


class SqlExecutor:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_one(self, query: str, values: dict) -> RowMapping | None:
        result = await self.session.execute(text(query), values)
        return result.mappings().first()

    async def fetch_all(self, query: str, values: dict) -> list[RowMapping]:
        result = await self.session.execute(text(query), values)
        return list(result.mappings().all())

    async def execute(self, query: str, values: dict) -> None:
        await self.session.execute(text(query), values)
```

- [ ] **Step 3: Verify import**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run python -c "from event_notifier.adapters.sql import SqlExecutor; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add event_notifier/adapters/sql.py event_notifier/interfaces/sql.py
git commit -m "feat(event-notifier): add SqlExecutor and ISqlExecutor protocol"
```

---

### Task 6: Rewrite NotificationRepository to use SqlExecutor

**Files:**
- Modify: `event_notifier/db/repository.py`

The repository currently uses `asyncpg.Pool` with `$1`, `$2` positional parameters. SQLAlchemy `text()` uses `:param_name` named parameters. All queries must be converted.

- [ ] **Step 1: Rewrite repository**

Replace the entire content of `event_notifier/db/repository.py`:

```python
"""SqlExecutor-based implementation of INotificationRepository."""

import json

import structlog

from event_notifier.domain.models.notification import OutboxRecord, RoutingRule
from event_notifier.interfaces.sql import ISqlExecutor

logger = structlog.get_logger(__name__)


class NotificationRepository:
    def __init__(self, sql: ISqlExecutor) -> None:
        self._sql = sql

    async def get_routing_rules(self, event_type: str) -> list[RoutingRule]:
        rows = await self._sql.fetch_all(
            "SELECT event_type, recipient_field, recipient_role "
            "FROM routing_rules WHERE event_type = :event_type AND active = TRUE",
            {"event_type": event_type},
        )
        return [
            RoutingRule(
                event_type=row["event_type"],
                recipient_field=row["recipient_field"],
                recipient_role=row["recipient_role"],
            )
            for row in rows
        ]

    async def is_processed(self, cloud_event_id: str) -> bool:
        row = await self._sql.fetch_one(
            "SELECT 1 FROM processed_events WHERE cloud_event_id = :cloud_event_id",
            {"cloud_event_id": cloud_event_id},
        )
        return row is not None

    async def write_outbox_atomically(
        self,
        cloud_event_id: str,
        records: list[dict],
    ) -> None:
        """Insert outbox records and mark event as processed.

        Transaction management is handled by the AsyncSession in the DI scope.
        """
        await self._sql.execute(
            "INSERT INTO processed_events (cloud_event_id) VALUES (:cloud_event_id) ON CONFLICT DO NOTHING",
            {"cloud_event_id": cloud_event_id},
        )
        for rec in records:
            await self._sql.execute(
                """
                INSERT INTO notification_outbox
                    (idempotency_key, cloud_event_id, booking_id, user_id,
                     recipient_address, recipient_role, channel, event_type, template_context)
                VALUES (:idempotency_key, :cloud_event_id, :booking_id, :user_id,
                        :recipient_address, :recipient_role, :channel, :event_type,
                        :template_context::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                {
                    "idempotency_key": rec["idempotency_key"],
                    "cloud_event_id": rec["cloud_event_id"],
                    "booking_id": rec["booking_id"],
                    "user_id": rec["user_id"],
                    "recipient_address": rec["recipient_address"],
                    "recipient_role": rec["recipient_role"],
                    "channel": rec["channel"],
                    "event_type": rec["event_type"],
                    "template_context": json.dumps(rec["template_context"]),
                },
            )
        await self._sql.session.commit()
        logger.debug("Outbox written atomically", cloud_event_id=cloud_event_id, count=len(records))

    async def fetch_pending_outbox(self, batch_size: int = 10) -> list[OutboxRecord]:
        rows = await self._sql.fetch_all(
            """
            UPDATE notification_outbox
            SET status = 'processing', updated_at = NOW()
            WHERE id IN (
                SELECT id FROM notification_outbox
                WHERE status = 'pending' AND scheduled_at <= NOW()
                ORDER BY scheduled_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id::text, cloud_event_id, booking_id, user_id,
                      recipient_address, recipient_role, channel, event_type,
                      template_context, retry_count, max_retries
            """,
            {"batch_size": batch_size},
        )
        await self._sql.session.commit()
        return [
            OutboxRecord(
                id=row["id"],
                cloud_event_id=row["cloud_event_id"],
                booking_id=row["booking_id"],
                user_id=row["user_id"],
                recipient_address=row["recipient_address"],
                recipient_role=row["recipient_role"],
                channel=row["channel"],
                event_type=row["event_type"],
                template_context=dict(row["template_context"]) if row["template_context"] else {},
                retry_count=row["retry_count"],
                max_retries=row["max_retries"],
            )
            for row in rows
        ]

    async def mark_delivered(self, record_id: str) -> None:
        await self._sql.execute(
            "UPDATE notification_outbox SET status='delivered', updated_at=NOW() WHERE id=:id::uuid",
            {"id": record_id},
        )
        await self._sql.session.commit()

    async def mark_retry(self, record_id: str, retry_count: int, delay_seconds: int) -> None:
        await self._sql.execute(
            """
            UPDATE notification_outbox
            SET retry_count = :retry_count,
                scheduled_at = NOW() + (:delay || ' seconds')::interval,
                status = 'pending',
                updated_at = NOW()
            WHERE id = :id::uuid
            """,
            {"id": record_id, "retry_count": retry_count, "delay": str(delay_seconds)},
        )
        await self._sql.session.commit()

    async def mark_failed(self, record_id: str) -> None:
        await self._sql.execute(
            "UPDATE notification_outbox SET status='failed', updated_at=NOW() WHERE id=:id::uuid",
            {"id": record_id},
        )
        await self._sql.session.commit()

    async def cleanup_processed_events(self, days: int = 7) -> None:
        """Delete processed_events older than the specified number of days."""
        await self._sql.execute(
            "DELETE FROM processed_events WHERE processed_at < NOW() - (:days || ' days')::interval",
            {"days": str(days)},
        )
        await self._sql.session.commit()
        logger.info("Cleaned up processed_events", older_than_days=days)
```

- [ ] **Step 2: Verify the module imports**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run python -c "from event_notifier.db.repository import NotificationRepository; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add event_notifier/db/repository.py
git commit -m "refactor(event-notifier): rewrite NotificationRepository to use SqlExecutor"
```

---

### Task 7: Rewrite IoC container (asyncpg → SQLAlchemy)

**Files:**
- Modify: `event_notifier/ioc.py`

- [ ] **Step 1: Rewrite ioc.py**

Replace the entire content of `event_notifier/ioc.py`:

```python
"""Dishka DI container for event-notifier."""

from collections.abc import AsyncGenerator

import httpx
import structlog
from dishka import Provider, Scope, provide
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from event_notifier.adapters.consumer import NotificationConsumer
from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.adapters.sql import SqlExecutor
from event_notifier.application.use_cases.process_domain_event import ProcessDomainEventUseCase
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.domain.models.notification import ChannelType
from event_notifier.infrastructure.channels.email import EmailChannel
from event_notifier.infrastructure.channels.telegram import TelegramChannel
from event_notifier.infrastructure.users_client import UsersClient
from event_notifier.interfaces.channels import INotificationChannel
from event_notifier.interfaces.sql import ISqlExecutor

logger = structlog.get_logger(__name__)


class AppProvider(Provider):
    @provide(scope=Scope.APP)
    def provide_settings(self) -> Settings:
        return Settings()

    @provide(scope=Scope.APP)
    async def provide_sessionmaker(self, settings: Settings) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
        engine = create_async_engine(
            str(settings.database_url),
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
        yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        await engine.dispose()

    @provide(scope=Scope.APP)
    async def provide_session(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> AsyncGenerator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    @provide(scope=Scope.APP)
    def provide_sql_executor(self, session: AsyncSession) -> ISqlExecutor:
        return SqlExecutor(session)

    @provide(scope=Scope.APP)
    def provide_repository(self, sql: ISqlExecutor) -> NotificationRepository:
        return NotificationRepository(sql=sql)

    @provide(scope=Scope.APP)
    def provide_exchange(self, settings: Settings) -> RabbitExchange:
        return RabbitExchange(name=settings.rabbit_exchange, type=ExchangeType.TOPIC, durable=True)

    @provide(scope=Scope.APP)
    def provide_broker(self, settings: Settings) -> RabbitBroker:
        return RabbitBroker(str(settings.rabbit_url))

    @provide(scope=Scope.APP)
    async def provide_users_client(self, settings: Settings) -> AsyncGenerator[UsersClient]:
        async with AsyncClient(
            base_url=str(settings.event_users_url),
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0),
        ) as client:
            yield UsersClient(http_client=client, api_token=settings.event_users_token)

    @provide(scope=Scope.APP)
    async def provide_email_channel(self, settings: Settings) -> AsyncGenerator[EmailChannel]:
        async with AsyncClient(
            base_url="https://go.unisender.ru",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0),
        ) as client:
            yield EmailChannel(
                http_client=client,
                api_key=settings.unisender_api_key,
                from_email=settings.unisender_from_email,
                from_name=settings.unisender_from_name,
            )

    @provide(scope=Scope.APP)
    async def provide_telegram_channel(self, settings: Settings) -> AsyncGenerator[TelegramChannel]:
        async with AsyncClient(
            base_url="https://api.telegram.org",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0),
        ) as client:
            yield TelegramChannel(http_client=client, bot_token=settings.telegram_bot_token)

    @provide(scope=Scope.APP)
    def provide_use_case(
        self,
        repository: NotificationRepository,
        users_client: UsersClient,
    ) -> ProcessDomainEventUseCase:
        return ProcessDomainEventUseCase(
            repository=repository,
            users_client=users_client,
        )

    @provide(scope=Scope.APP)
    def provide_outbox_sender(
        self,
        repository: NotificationRepository,
        email_channel: EmailChannel,
        telegram_channel: TelegramChannel,
    ) -> OutboxSender:
        channels: dict[ChannelType, INotificationChannel] = {
            ChannelType.EMAIL: email_channel,
            ChannelType.TELEGRAM: telegram_channel,
            # ChannelType.PUSH: push_channel  — включить после настройки FCM
        }
        return OutboxSender(repository=repository, channels=channels)

    @provide(scope=Scope.APP)
    def provide_consumer(
        self,
        broker: RabbitBroker,
        exchange: RabbitExchange,
        settings: Settings,
        use_case: ProcessDomainEventUseCase,
    ) -> NotificationConsumer:
        return NotificationConsumer(
            broker=broker,
            exchange=exchange,
            queue_name=settings.notifications_queue,
            use_case=use_case,
        )
```

- [ ] **Step 2: Rewrite main.py — remove asyncpg and create_tables**

Replace the entire content of `event_notifier/main.py`:

```python
"""FastAPI application entry point for event-notifier."""

import asyncio
from contextlib import asynccontextmanager
from logging import getLevelNamesMapping
from typing import TYPE_CHECKING

import structlog
from dishka import make_async_container
from fastapi import FastAPI

from event_notifier.adapters.consumer import NotificationConsumer
from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.ioc import AppProvider
from event_notifier.logger import setup_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    container = make_async_container(AppProvider())

    settings = await container.get(Settings)
    log_level = getLevelNamesMapping().get(settings.log_level.upper(), 20)
    setup_logger(log_level=log_level, console_render=settings.debug)

    logger.info("Starting event-notifier", log_level=settings.log_level)

    # Start RabbitMQ consumer
    consumer = await container.get(NotificationConsumer)
    await consumer.start()

    # Start OutboxSender as background asyncio task
    outbox_sender = await container.get(OutboxSender)
    sender_task = asyncio.create_task(outbox_sender.start(), name="outbox-sender")

    # Start periodic cleanup of processed_events table
    repository = await container.get(NotificationRepository)

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(3600)  # every hour
            try:
                await repository.cleanup_processed_events(days=7)
            except Exception:
                logger.exception("processed_events cleanup failed")

    cleanup_task = asyncio.create_task(_cleanup_loop(), name="processed-events-cleanup")

    logger.info("event-notifier ready")

    yield

    logger.info("Shutting down event-notifier")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    outbox_sender.stop()
    sender_task.cancel()
    try:
        await sender_task
    except asyncio.CancelledError:
        pass

    await consumer.stop()
    await container.close()


app = FastAPI(title="event-notifier", version="0.3.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

- [ ] **Step 3: Delete schema.py**

Run: `rm /Users/alexandrlelikov/PycharmProjects/events/event-notifier/event_notifier/db/schema.py`

- [ ] **Step 4: Verify imports**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run python -c "from event_notifier.ioc import AppProvider; from event_notifier.main import app; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add event_notifier/ioc.py event_notifier/main.py
git rm event_notifier/db/schema.py
git commit -m "refactor(event-notifier): replace asyncpg pool with SQLAlchemy AsyncSession in IoC"
```

---

### Task 8: Integrate TriggerEvent enum into channel template maps

**Files:**
- Modify: `event_notifier/infrastructure/channels/email.py:12-20`
- Modify: `event_notifier/infrastructure/channels/telegram.py:12-19`
- Modify: `event_notifier/infrastructure/channels/push.py:12-19`
- Modify: `event_notifier/interfaces/channels.py:10`
- Modify: `event_notifier/adapters/outbox_sender.py:68`

- [ ] **Step 1: Update email.py template map**

In `event_notifier/infrastructure/channels/email.py`, replace:

```python
# Maps trigger_event → UniSender template code.
_TEMPLATE_MAP: dict[str, str] = {
    "BOOKING_CREATED": "booking_created",
    "BOOKING_CANCELLED": "booking_cancelled",
    "BOOKING_RESCHEDULED": "booking_rescheduled",
    "BOOKING_REASSIGNED": "booking_reassigned",
    "BOOKING_REMINDER": "booking_reminder",
    "BOOKING_REJECTED": "booking_rejected",
}
```

with:

```python
from event_schemas.types import TriggerEvent

# Maps trigger_event → UniSender template code.
_TEMPLATE_MAP: dict[TriggerEvent, str] = {
    TriggerEvent.BOOKING_CREATED: "booking_created",
    TriggerEvent.BOOKING_CANCELLED: "booking_cancelled",
    TriggerEvent.BOOKING_RESCHEDULED: "booking_rescheduled",
    TriggerEvent.BOOKING_REASSIGNED: "booking_reassigned",
    TriggerEvent.BOOKING_REMINDER: "booking_reminder",
    TriggerEvent.BOOKING_REJECTED: "booking_rejected",
}
```

Also update the `send` method signature and template lookup — change `trigger_event: str` to `trigger_event: TriggerEvent`:

```python
    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
```

- [ ] **Step 2: Update telegram.py template map**

In `event_notifier/infrastructure/channels/telegram.py`, add import and replace map:

```python
from event_schemas.types import TriggerEvent

_MESSAGE_TEMPLATES: dict[TriggerEvent, str] = {
    TriggerEvent.BOOKING_CREATED: "Новая встреча забронирована.",
    TriggerEvent.BOOKING_CANCELLED: "Встреча отменена.",
    TriggerEvent.BOOKING_RESCHEDULED: "Встреча перенесена.",
    TriggerEvent.BOOKING_REASSIGNED: "Встреча переназначена.",
    TriggerEvent.BOOKING_REMINDER: "Напоминание о встрече.",
    TriggerEvent.BOOKING_REJECTED: "Бронирование отклонено.",
}
```

Also update `send` method: `trigger_event: str` → `trigger_event: TriggerEvent`.

And change the fallback line:

```python
text = _MESSAGE_TEMPLATES.get(trigger_event, f"Уведомление: {trigger_event}")
```

stays the same — `TriggerEvent` is a `str` subclass, so `.get()` and f-string work.

- [ ] **Step 3: Update push.py template map**

In `event_notifier/infrastructure/channels/push.py`, add import and replace map:

```python
from event_schemas.types import TriggerEvent

_PUSH_TITLES: dict[TriggerEvent, str] = {
    TriggerEvent.BOOKING_CREATED: "Новая встреча",
    TriggerEvent.BOOKING_CANCELLED: "Встреча отменена",
    TriggerEvent.BOOKING_RESCHEDULED: "Встреча перенесена",
    TriggerEvent.BOOKING_REASSIGNED: "Встреча переназначена",
    TriggerEvent.BOOKING_REMINDER: "Напоминание",
    TriggerEvent.BOOKING_REJECTED: "Бронирование отклонено",
}
```

Also update `send` method: `trigger_event: str` → `trigger_event: TriggerEvent`.

- [ ] **Step 4: Update INotificationChannel protocol**

In `event_notifier/interfaces/channels.py`, change:

```python
from typing import Any, Protocol

from event_schemas.types import TriggerEvent

from event_notifier.domain.models.notification import ChannelContact, DeliveryResult


class INotificationChannel(Protocol):
    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult: ...
```

- [ ] **Step 5: Update event_types.py type annotation**

In `event_notifier/event_types.py`, update the mapping type:

```python
"""Event type constants for event-notifier."""

from event_schemas.types import EventType, TriggerEvent

NOTIFIER_SOURCE = "event-notifier"

# Mapping from CloudEvent type to trigger_event used by channel adapters
DOMAIN_EVENT_TO_TRIGGER: dict[str, TriggerEvent] = {
    EventType.BOOKING_CREATED: TriggerEvent.BOOKING_CREATED,
    EventType.BOOKING_CANCELLED: TriggerEvent.BOOKING_CANCELLED,
    EventType.BOOKING_RESCHEDULED: TriggerEvent.BOOKING_RESCHEDULED,
    EventType.BOOKING_REASSIGNED: TriggerEvent.BOOKING_REASSIGNED,
    EventType.BOOKING_REMINDER_SENT: TriggerEvent.BOOKING_REMINDER,
}
```

- [ ] **Step 6: Update outbox_sender.py trigger_event usage**

In `event_notifier/adapters/outbox_sender.py`, the line:

```python
trigger_event = DOMAIN_EVENT_TO_TRIGGER.get(record.event_type, record.event_type)
```

Now `DOMAIN_EVENT_TO_TRIGGER.get()` returns `TriggerEvent | None`. Update to handle the `None` case:

```python
trigger_event = DOMAIN_EVENT_TO_TRIGGER.get(record.event_type)
if trigger_event is None:
    logger.error("No trigger_event mapping for event_type, marking failed", event_type=record.event_type, id=record.id)
    await self._repository.mark_failed(record.id)
    return
```

- [ ] **Step 7: Run tests to check nothing broke**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run pytest -x -v`
Expected: All tests pass (tests use string `"BOOKING_CREATED"` which equals `TriggerEvent.BOOKING_CREATED` since it's a `str` enum).

- [ ] **Step 8: Commit**

```bash
git add event_notifier/infrastructure/channels/email.py event_notifier/infrastructure/channels/telegram.py event_notifier/infrastructure/channels/push.py event_notifier/interfaces/channels.py event_notifier/event_types.py event_notifier/adapters/outbox_sender.py
git commit -m "refactor(event-notifier): use TriggerEvent enum from event-schemas for template maps"
```

---

### Task 9: Replace "volunteer" with RecipientRole.ORGANIZER across codebase

**Files:**
- Modify: `event_notifier/domain/models/notification.py`
- Modify: `event_notifier/domain/services/routing.py`
- Modify: `event_notifier/interfaces/users_client.py`
- Modify: `event_notifier/infrastructure/users_client.py`
- Modify: `event_notifier/application/use_cases/process_domain_event.py`
- Modify: `tests/domain/test_routing_service.py`
- Modify: `tests/application/test_process_domain_event.py`
- Modify: `tests/infrastructure/test_outbox_sender.py`
- Modify: `tests/infrastructure/test_users_client.py`

- [ ] **Step 1: Update domain models to use RecipientRole**

In `event_notifier/domain/models/notification.py`, add import and update type hints:

```python
"""Domain models for notification dispatch — pure dataclasses, no infrastructure deps."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from event_schemas.types import RecipientRole


class ChannelType(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    PUSH = "push"


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Parsed incoming CloudEvent (domain event from booking service)."""

    event_id: str
    event_type: str
    source: str
    booking_id: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """A single routing rule from the DB."""

    event_type: str
    recipient_field: str
    recipient_role: str  # stored as string in DB, converted to RecipientRole at use site


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """A record from the notification_outbox table."""

    id: str
    cloud_event_id: str
    booking_id: str
    user_id: str
    recipient_address: str
    recipient_role: str
    channel: str
    event_type: str
    template_context: dict[str, Any]
    retry_count: int
    max_retries: int


@dataclass(frozen=True, slots=True)
class ChannelContact:
    """A resolved channel contact for a recipient."""

    channel: ChannelType
    contact_id: str
    user_id: str
    role: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Result of a single channel delivery attempt."""

    channel: ChannelType
    success: bool
    message_id: str | None = None
    error: str | None = None
```

Note: We keep `str` types in domain models for simplicity — they store `RecipientRole` string values (`"organizer"`, `"client"`). The `RecipientRole` enum is used at call sites for type safety and as constants.

- [ ] **Step 2: Update routing.py**

No code changes needed — `apply_routing_rules` returns `(user_id, role)` tuples where `role` comes from `RoutingRule.recipient_role` (a string). The DB now stores `"organizer"` instead of `"volunteer"`, so the values are already correct.

- [ ] **Step 3: Update users_client.py — use RecipientRole values**

In `event_notifier/infrastructure/users_client.py`, no functional changes needed. The `role` parameter is a `str` that now receives `"organizer"` or `"client"` values from the DB routing rules. The users client passes it through as-is.

- [ ] **Step 4: Update test files — replace "volunteer" with "organizer"**

In `tests/domain/test_routing_service.py`:

Replace all occurrences of `"volunteer"` with `"organizer"` and `"volunteer_id"` with `"organizer_id"`:

```python
from event_notifier.domain.models.notification import RoutingRule
from event_notifier.domain.services.routing import apply_routing_rules, extract_field_value


def test_extract_top_level_field():
    data = {"organizer_id": "uuid-org-001"}
    assert extract_field_value(data, "organizer_id") == "uuid-org-001"


def test_extract_nested_field():
    data = {"user": {"id": "uuid-org-001"}}
    assert extract_field_value(data, "user.id") == "uuid-org-001"


def test_extract_missing_field_returns_none():
    data = {"user": {"name": "Bob"}}
    assert extract_field_value(data, "user.id") is None


def test_extract_non_string_returns_none():
    data = {"count": 42}
    assert extract_field_value(data, "count") is None


def test_apply_routing_rules_booking_created():
    rules = [
        RoutingRule(event_type="booking.created", recipient_field="organizer_id", recipient_role="organizer"),
        RoutingRule(event_type="booking.created", recipient_field="client_id", recipient_role="client"),
    ]
    data = {"organizer_id": "uuid-org-001", "client_id": "uuid-cli-001"}
    recipients = apply_routing_rules(event_type="booking.created", event_data=data, routing_rules=rules)
    assert len(recipients) == 2
    assert ("uuid-org-001", "organizer") in recipients
    assert ("uuid-cli-001", "client") in recipients


def test_apply_routing_rules_skips_missing_fields():
    rules = [
        RoutingRule(event_type="booking.cancelled", recipient_field="organizer_id", recipient_role="organizer"),
        RoutingRule(event_type="booking.cancelled", recipient_field="client_id", recipient_role="client"),
    ]
    data = {"organizer_id": "uuid-org-001", "cancellation_reason": "test"}
    recipients = apply_routing_rules(event_type="booking.cancelled", event_data=data, routing_rules=rules)
    assert recipients == [("uuid-org-001", "organizer")]
```

In `tests/application/test_process_domain_event.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from event_notifier.application.use_cases.process_domain_event import ProcessDomainEventUseCase
from event_notifier.domain.models.notification import (
    ChannelContact,
    ChannelType,
    DomainEvent,
    RoutingRule,
)


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.get_routing_rules = AsyncMock(
        return_value=[
            RoutingRule(event_type="booking.created", recipient_field="organizer_id", recipient_role="organizer"),
            RoutingRule(event_type="booking.created", recipient_field="client_id", recipient_role="client"),
        ]
    )
    repo.is_processed = AsyncMock(return_value=False)
    repo.write_outbox_atomically = AsyncMock()
    return repo


@pytest.fixture
def mock_users_client():
    client = MagicMock()
    client.get_contacts_by_id = AsyncMock(
        side_effect=lambda *, user_id, role: [
            ChannelContact(channel=ChannelType.EMAIL, contact_id=f"{user_id}@example.com", user_id=user_id, role=role),
            ChannelContact(channel=ChannelType.TELEGRAM, contact_id="chat-123", user_id=user_id, role=role),
        ]
    )
    return client


@pytest.fixture
def event():
    return DomainEvent(
        event_id="evt-001",
        event_type="booking.created",
        source="booking",
        booking_id="booking-abc",
        data={"organizer_id": "uuid-org-001", "client_id": "uuid-cli-001"},
    )


@pytest.mark.asyncio
async def test_writes_outbox_records_for_all_contacts(mock_repository, mock_users_client, event):
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    mock_repository.write_outbox_atomically.assert_awaited_once()
    _, call_kwargs = mock_repository.write_outbox_atomically.call_args
    records = call_kwargs["records"]
    # 2 recipients * 2 channels each = 4 outbox records
    assert len(records) == 4
    channels = {r["channel"] for r in records}
    assert "email" in channels
    assert "telegram" in channels


@pytest.mark.asyncio
async def test_skips_already_processed_events(mock_repository, mock_users_client, event):
    mock_repository.is_processed = AsyncMock(return_value=True)
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    mock_repository.write_outbox_atomically.assert_not_awaited()
    mock_users_client.get_contacts_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_event_with_no_routing_rules(mock_repository, mock_users_client, event):
    mock_repository.get_routing_rules = AsyncMock(return_value=[])
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    mock_repository.write_outbox_atomically.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotency_key_format(mock_repository, mock_users_client, event):
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    _, call_kwargs = mock_repository.write_outbox_atomically.call_args
    records = call_kwargs["records"]
    keys = [r["idempotency_key"] for r in records]
    # format: "{event_id}:{user_id}:{channel}"
    assert any("evt-001:uuid-org-001:email" == k for k in keys)
    assert any("evt-001:uuid-org-001:telegram" == k for k in keys)
```

In `tests/infrastructure/test_outbox_sender.py`:

Replace `"volunteer"` with `"organizer"` in the `make_record` defaults:

```python
    defaults = {
        "id": "record-uuid-1",
        "cloud_event_id": "evt-001",
        "booking_id": "booking-abc",
        "user_id": "uuid-user-001",
        "recipient_address": "user@example.com",
        "recipient_role": "organizer",
        "channel": "email",
        "event_type": "booking.created",
        "template_context": {"organizer_id": "uuid-user-001"},
        "retry_count": 0,
        "max_retries": 5,
    }
```

In `tests/infrastructure/test_users_client.py`:

Replace `role="volunteer"` with `role="organizer"` in:
- Line 88 (in mock response): `"role": "organizer"` (already `"organizer"` in the mock data, but the call uses `role="volunteer"`)
- Line 93: `contacts = await client.get_contacts_by_id(user_id=user_id, role="organizer")`

- [ ] **Step 5: Run all tests**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run pytest -x -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add event_notifier/domain/models/notification.py tests/
git commit -m "refactor(event-notifier): replace volunteer with organizer using RecipientRole convention"
```

---

### Task 10: Run full lint + test suite

**Files:** None (verification only)

- [ ] **Step 1: Run ruff lint**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run ruff check --fix .`
Expected: No errors (or auto-fixed).

- [ ] **Step 2: Run ruff format**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run ruff format .`
Expected: Files formatted.

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/alexandrlelikov/PycharmProjects/events/event-notifier && uv run pytest -x -v`
Expected: All tests pass.

- [ ] **Step 4: Commit any lint fixes**

```bash
git add -u
git commit -m "style(event-notifier): apply ruff formatting"
```

---

### Task 11: Update documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/SERVICE_OVERVIEW.md`

- [ ] **Step 1: Update CLAUDE.md**

Update the Layer Map table to reflect new files:
- Add `adapters/sql.py` → `SqlExecutor` wrapping `AsyncSession`
- Add `interfaces/sql.py` → `ISqlExecutor` protocol
- Add `db/base.py` → `DeclarativeBase`
- Add `db/models.py` → ORM models (Alembic only)
- Remove `db/schema.py` reference
- Update `db/repository.py` description: `asyncpg wrapper` → `SqlExecutor-based repository`
- Update commands section: add `uv run alembic upgrade head` and `uv run alembic revision --autogenerate -m "description"`
- Update Required Environment Variables: note `DATABASE_URL` uses `postgresql+asyncpg://` format

- [ ] **Step 2: Update docs/SERVICE_OVERVIEW.md**

Update architecture section to reflect:
- SQLAlchemy + Alembic instead of raw asyncpg
- `SqlExecutor` pattern
- `RecipientRole` from event-schemas
- `TriggerEvent` enum usage in template maps

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/SERVICE_OVERVIEW.md
git commit -m "docs(event-notifier): update documentation for Alembic and event-schemas integration"
```
