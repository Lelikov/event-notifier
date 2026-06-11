# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                      # install deps
uv run pytest                # run all tests
uv run pytest tests/application/   # run a specific test directory
uv run pytest tests/application/test_process_notification_command.py::test_writes_email_and_telegram_records  # single test
ruff check --fix .           # lint
ruff format .                # format
pre-commit run --all-files   # all hooks
uvicorn event_notifier.main:app --reload  # run locally
uv run alembic upgrade head  # apply migrations
uv run alembic revision --autogenerate -m "description"  # generate migration
```

## Architecture

This service is a **notification dispatcher**: it consumes `notification.send_requested`
CloudEvents from RabbitMQ, resolves recipient channels, fans out via a transactional
outbox to Email (UniSender Go) and Telegram, and publishes
`notification.*.message_sent` delivery-result events back to event-receiver over HTTP.

### Request Flow

```
RabbitMQ queue: events.notification.commands   (spec: event_schemas.queues; DLQ via events.dlx)
        │ binary CloudEvent, body = {original, normalized} envelope
        ▼
NotificationConsumer (adapters/consumer.py)
  – EventEnvelope → NotificationCommandPayload (trigger_event, recipients, template_data)
  – merges user_id/time_zone from normalized.participants by email
  – ack policy: poison → RejectMessage (DLQ); transient → backoff then NackMessage(requeue)
        │
        ▼
ProcessNotificationCommandUseCase (application/use_cases/process_notification_command.py)
  – email contact always from the command recipient; user_id only ADDS channels
    (UsersClient GET /api/users/id/{id}: 404 → email-only, 5xx/transport → raise → retry)
  – per-recipient localization (start_time_local/end_time_local/time_zone)
  – processed_events claim + outbox insert in ONE transaction (idempotent)
        │
        ▼
notification_outbox (PostgreSQL)
        │ poll 1s (idle backoff → 30s); stale 'processing' reaped after 300s
        ▼
OutboxSender (adapters/outbox_sender.py)
  – permanent failure → 'failed'; transient → capped exponential backoff (max 10 retries)
  – success → 'delivered' + DeliveryResultPublisher → POST CloudEvent to event-receiver
        │
        ├──► EmailChannel    → UniSender Go (template UUIDs from UNISENDER_TEMPLATE_IDS)
        ├──► TelegramChannel → Bot API sendMessage (Jinja2: templates/telegram/<TRIGGER>.j2)
        └──► (PushChannel    – implemented, not registered: FCM credentials pending)
```

### Layer Map

| Layer | Path | Responsibility |
|---|---|---|
| Entry point | `main.py` | FastAPI lifespan: consumer, outbox-sender task, processed_events TTL loop, deep `/health` |
| DI | `ioc.py` | Dishka `AppProvider` (APP scope; per-operation sessions via sessionmaker) |
| Config | `config.py` | `pydantic-settings`; `DATABASE_URL` must be `postgresql+asyncpg://` |
| DB models | `db/models.py` | ORM only for Alembic autogenerate (not used for queries) |
| Repository | `db/repository.py` | raw `text()` SQL: atomic claim+write, SKIP LOCKED batch claim, reaper, retry/fail marks |
| SQL executor | `adapters/sql.py` | fresh `AsyncSession` per operation; `transaction()` for atomic units |
| Domain | `domain/models/notification.py`, `domain/localization.py` | frozen DTOs; pure per-recipient time localization |
| Use case | `application/use_cases/process_notification_command.py` | contact resolution → outbox write |
| Consumer | `adapters/consumer.py` | FastStream subscriber (raw message via Context), ack policy, DLX topology |
| Outbox sender | `adapters/outbox_sender.py` | polling, permanent/transient classification, result publishing |
| Result publisher | `adapters/result_publisher.py` | fire-and-forget `notification.*.message_sent` POST |
| Interfaces | `interfaces/` | `INotificationChannel`, `IUsersClient`, `ISqlExecutor`, `INotificationRepository`, `IDeliveryResultPublisher` protocols |
| Channels | `infrastructure/channels/` | UniSender Go, Telegram, FCM (unregistered) |
| Templates | `event_notifier/templates/telegram/` | Jinja2 message bodies (one file per TriggerEvent) |

### Adding a New Channel

1. Implement the `INotificationChannel` protocol (`interfaces/channels.py`): async `send()`
   returning `DeliveryResult` with correct `retryable` classification
   (408/429/5xx/transport → True; other 4xx, missing template → False).
2. Register a provider in `ioc.py` and add it to the `channels` dict in `provide_outbox_sender`.
3. Add the `ChannelType` enum value if needed; extend `_resolve_contacts` in the use case
   so the channel gets contacts from event-users data.
4. Add templates (config-driven ids or `templates/` files — user-facing text never lives in code).
5. Map the channel in `result_publisher._CHANNEL_TO_EVENT_TYPE` for delivery results.

### Hard External Contracts (do not change)

- **UniSender Go**: `POST /ru/transactional/api/v1/email/send.json`, API key in `X-API-KEY`
  header, `message.template_id` = provisioned template UUID, flat `global_substitutions`.
- **Telegram Bot API**: `POST /bot{token}/sendMessage` with `chat_id`/`text`/`parse_mode`.

### Required Environment Variables

See `.env.example`. Required: `DATABASE_URL` (asyncpg), `EVENT_USERS_URL`,
`EVENT_USERS_TOKEN`, `UNISENDER_API_KEY`, `UNISENDER_FROM_EMAIL`, `TELEGRAM_BOT_TOKEN`.
Optional: `EVENTS_ENDPOINT_URL`/`EVENTS_API_KEY` (delivery results),
`UNISENDER_TEMPLATE_IDS` (JSON dict), `CONSUMER_PREFETCH_COUNT`, `GRACEFUL_TIMEOUT`, FCM vars.

### Test Approach

`pytest-asyncio` (`asyncio_mode = "auto"`), `respx` for HTTP, `TestRabbitBroker` for the
FastStream wire contract, fake `ISqlExecutor` for repository SQL tests, real Dishka
container resolution in `tests/test_ioc.py`. No real external connections.

## Service Documentation

- `docs/SERVICE_OVERVIEW.md` — architecture, maturity, known limitations
- `docs/API_CONTRACTS.md` — queue spec, envelope, ack policy, outbox lifecycle, result events
- `docs/DEPENDENCIES.md` — external dependencies and failure modes
- `docs/AUDIT.md` — audit-v2 findings → fixes ledger

Cross-service architecture docs (message contracts, system topology, onboarding) are in `../docs/`.

## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
