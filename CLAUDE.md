# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                      # install deps
uv run pytest                # run all tests
uv run pytest tests/application/   # run a specific test directory
uv run pytest tests/application/test_process_domain_event.py::test_writes_outbox_records_for_all_contacts  # single test
ruff check --fix .           # lint
ruff format .                # format
pre-commit run --all-files   # all hooks
uvicorn event_notifier.main:app --reload  # run locally
uv run alembic upgrade head  # apply migrations
uv run alembic revision --autogenerate -m "description"  # generate migration
```

## Architecture

This service is a **notification dispatcher**: it consumes `notification.send_requested` CloudEvents from RabbitMQ, resolves recipient contacts, and fans out to delivery channels (Email/Telegram/Push). It publishes `notification.*.message_sent` result events back to `event-receiver`.

### Request Flow

```
RabbitMQ queue: events.notification.commands
        │ (CloudEvent: booking.created / booking.cancelled / …)
        ▼
NotificationConsumer (adapters/consumer.py)
  – parses CloudEvent, validates event_type against DOMAIN_EVENT_TO_TRIGGER
  – constructs DomainEvent frozen dataclass
        │
        ▼
ProcessDomainEventUseCase (application/use_cases/process_domain_event.py)
  – idempotency check via processed_events table
  – loads routing_rules from DB → extracts (user_id, role) pairs
  – per recipient: UsersClient.get_contacts_by_id() → list[ChannelContact]
  – writes outbox records + marks processed atomically
        │
        ▼
notification_outbox table (PostgreSQL)
        │ (poll every 1s)
        ▼
OutboxSender (adapters/outbox_sender.py)
  – fetch pending, resolve channel adapter, call channel.send()
        │
        ├──► EmailChannel   → UniSender Go API (TriggerEvent → template_id)
        ├──► TelegramChannel → Bot API /sendMessage (TriggerEvent → message text)
        └──► (PushChannel   – wired but commented out pending FCM credentials)
```

**Key design principle**: The consumer maps CloudEvent `type` to a `TriggerEvent` enum (from `event-schemas`). Routing rules in the DB extract recipient UUIDs using dot-notation paths into event data. The use case calls `UsersClient` to resolve channel contacts per recipient, then writes delivery tasks to the transactional outbox.

### Layer Map

| Layer | Path | Responsibility |
|---|---|---|
| Entry point | `main.py` | FastAPI app + lifespan: starts consumer, outbox sender, `/health` endpoint |
| DI | `ioc.py` | Dishka `AppProvider` — SQLAlchemy engine/session, channels, use case wiring |
| Config | `config.py` | `pydantic-settings`, `.env` file, `DATABASE_URL` as `postgresql+asyncpg://` |
| DB base | `db/base.py` | SQLAlchemy `DeclarativeBase` |
| DB models | `db/models.py` | ORM models for Alembic migrations (not used for queries) |
| DB repository | `db/repository.py` | `NotificationRepository` — `text()` SQL queries via `SqlExecutor` |
| SQL executor | `adapters/sql.py` | `SqlExecutor` — thin wrapper over `AsyncSession` (fetch_one/fetch_all/execute) |
| Domain models | `domain/models/notification.py` | Frozen dataclasses: `DomainEvent`, `RoutingRule`, `OutboxRecord`, `ChannelContact`, `DeliveryResult`, `ChannelType` |
| Domain routing | `domain/services/routing.py` | `apply_routing_rules`, `extract_field_value` — pure functions |
| Use case | `application/use_cases/process_domain_event.py` | Orchestrates routing → contact resolution → outbox write |
| Consumer | `adapters/consumer.py` | FastStream `RabbitBroker`, CloudEvent parsing, event type filtering |
| Outbox sender | `adapters/outbox_sender.py` | Background polling + delivery + retry logic |
| Interfaces | `interfaces/` | `INotificationChannel`, `IUsersClient`, `ISqlExecutor` protocols |
| Channels | `infrastructure/channels/` | `EmailChannel` (UniSender Go), `TelegramChannel`, `PushChannel` (disabled) |
| Users client | `infrastructure/users_client.py` | GET `/users/{user_id}` on `event-users` |
| Event types | `event_types.py` | `DOMAIN_EVENT_TO_TRIGGER` mapping (`EventType` → `TriggerEvent` from event-schemas) |

### Adding a New Channel

1. Implement the `INotificationChannel` protocol (`interfaces/channels.py`): one async `send()` method returning `DeliveryResult`.
2. Register it in `ioc.py`: add a `provide_*_channel` method and add `ChannelType.X: channel` to the dict in `provide_use_case`.
3. Add the `ChannelType` enum value if not already present.
4. Map `trigger_event` strings → templates inside the channel implementation.

### Template Mapping

`trigger_event` is a `TriggerEvent` enum value (from `event-schemas`) like `TriggerEvent.BOOKING_CREATED`. Each channel maintains its own `_TEMPLATE_MAP` / `_MESSAGE_TEMPLATES` dict mapping `TriggerEvent` enum keys to provider-specific template codes or message bodies. Unknown `trigger_event` values return a `DeliveryResult(success=False)`.

### External Dependencies

| Service | How accessed | Fallback |
|---|---|---|
| `event-users` | HTTP GET `/api/users` (Bearer token) | Returns email-only contacts on any error |
| `event-receiver` | HTTP POST `/event/cloudevents` (JWT Bearer) | Fire-and-forget, errors are logged only |
| UniSender Go | HTTP POST `/ru/transactional/api/v1/email/send.json` | Returns `DeliveryResult(success=False)` |
| Telegram Bot API | HTTP POST `/bot{token}/sendMessage` | Returns `DeliveryResult(success=False)` |

### Required Environment Variables

```
DATABASE_URL                # postgresql+asyncpg://user:pass@host/db (required)
RABBIT_URL                  # amqp://... (default: amqp://guest:guest@localhost:5672/)
RABBIT_EXCHANGE             # default: "events"
EVENT_USERS_URL             # required
EVENT_USERS_TOKEN           # required
UNISENDER_API_KEY           # required
UNISENDER_FROM_EMAIL        # required
UNISENDER_FROM_NAME         # default: "Notifications"
TELEGRAM_BOT_TOKEN          # required
FCM_PROJECT_ID              # optional (PushChannel disabled)
FCM_SERVICE_ACCOUNT_JSON    # optional (PushChannel disabled)
```

### Test Approach

Tests use `pytest-asyncio` (`asyncio_mode = "auto"`) with `pytest-mock` and `respx`. Infrastructure tests mock HTTP via `respx`; use case tests use `unittest.mock.AsyncMock`. No real external connections in tests.

## Service Documentation

- `docs/SERVICE_OVERVIEW.md` — architecture, maturity, known issues
- `docs/API_CONTRACTS.md` — HTTP endpoints, request/response schemas
- `docs/DEPENDENCIES.md` — external service dependencies and failure modes
- `docs/AUDIT.md` — audit findings for this service

Cross-service architecture docs (message contracts, system topology, onboarding) are in `../docs/`.

<!-- code-review-graph MCP tools -->
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
