# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                      # install deps
uv run pytest                # run all tests
uv run pytest tests/application/   # run a specific test directory
uv run pytest tests/application/test_dispatch_notification.py::test_dispatches_to_all_resolved_channels  # single test
ruff check --fix .           # lint
ruff format .                # format
pre-commit run --all-files   # all hooks
uvicorn event_notifier.main:app --reload  # run locally
```

## Architecture

This service is a **notification dispatcher**: it consumes `notification.send_requested` CloudEvents from RabbitMQ, resolves recipient contacts, and fans out to delivery channels (Email/Telegram/Push). It publishes `notification.*.message_sent` result events back to `event-receiver`.

### Request Flow

```
RabbitMQ queue: events.notification.commands
        │ (CloudEvent: notification.send_requested)
        ▼
NotificationConsumer (adapters/consumer.py)
  – parses CloudEvent, extracts NotificationCommand
        │
        ▼
DispatchNotificationUseCase (application/use_cases/dispatch_notification.py)
  – for each recipient.email → UsersClient.get_contacts_by_email()
  – for each ChannelContact → INotificationChannel.send()
  – for each result → ResultEventPublisher.publish_delivery_result()
        │
        ├──► EmailChannel   → UniSender Go API (template_code from _TEMPLATE_MAP)
        ├──► TelegramChannel → Bot API /sendMessage (hardcoded message strings)
        └──► (PushChannel   – wired but commented out pending FCM credentials)
```

**Key design principle**: The consumer receives recipients as `{email, role}` tuples. The use case calls `UsersClient` to look up all channel contacts for that email — always including email itself, plus any telegram/push contacts from `event-users`. This fan-out happens per recipient.

### Layer Map

| Layer | Path | Responsibility |
|---|---|---|
| Entry point | `main.py` | FastAPI app + lifespan: starts consumer, `/health` endpoint |
| DI | `ioc.py` | Dishka `AppProvider` — all wiring at `Scope.APP` |
| Config | `config.py` | `pydantic-settings`, env prefix-less (no `NOTIFY_` prefix), `.env` file |
| Domain models | `domain/models/notification.py` | Frozen dataclasses: `NotificationCommand`, `ChannelContact`, `DeliveryResult` |
| Use case | `application/use_cases/dispatch_notification.py` | Orchestrates fan-out; catches exceptions per-channel |
| Consumer | `adapters/consumer.py` | FastStream `RabbitBroker`, `declare=False` (queue must pre-exist) |
| Interfaces | `interfaces/` | `INotificationChannel`, `IUsersClient`, `IResultEventPublisher` protocols |
| Channels | `infrastructure/channels/` | `EmailChannel` (UniSender Go), `TelegramChannel` |
| Users client | `infrastructure/users_client.py` | GET `/api/users?email=&role=&limit=1` on `event-users` |
| Publisher | `infrastructure/publisher.py` | POST `/event/cloudevents` on `event-receiver` (CloudEvents binary mode) |
| Event types | `event_types.py` | String constants for CloudEvent `type` field |

### Adding a New Channel

1. Implement the `INotificationChannel` protocol (`interfaces/channels.py`): one async `send()` method returning `DeliveryResult`.
2. Register it in `ioc.py`: add a `provide_*_channel` method and add `ChannelType.X: channel` to the dict in `provide_use_case`.
3. Add the `ChannelType` enum value if not already present.
4. Map `trigger_event` strings → templates inside the channel implementation.

### Template Mapping

`trigger_event` is a string like `"BOOKING_CREATED"` passed in the CloudEvent payload. Each channel maintains its own `_TEMPLATE_MAP` / `_MESSAGE_TEMPLATES` dict mapping these strings to provider-specific template codes or message bodies. Unknown `trigger_event` values return a `DeliveryResult(success=False)`.

### External Dependencies

| Service | How accessed | Fallback |
|---|---|---|
| `event-users` | HTTP GET `/api/users` (Bearer token) | Returns email-only contacts on any error |
| `event-receiver` | HTTP POST `/event/cloudevents` (JWT Bearer) | Fire-and-forget, errors are logged only |
| UniSender Go | HTTP POST `/ru/transactional/api/v1/email/send.json` | Returns `DeliveryResult(success=False)` |
| Telegram Bot API | HTTP POST `/bot{token}/sendMessage` | Returns `DeliveryResult(success=False)` |

### Required Environment Variables

```
RABBIT_URL                  # amqp://...
RABBIT_EXCHANGE             # default: "events"
NOTIFICATION_COMMANDS_QUEUE # default: "events.notification.commands"
EVENT_RECEIVER_URL          # required
EVENT_RECEIVER_JWT          # required
EVENT_USERS_URL             # required
EVENT_USERS_TOKEN           # required
UNISENDER_API_KEY           # required
UNISENDER_FROM_EMAIL        # required
UNISENDER_FROM_NAME         # default: "Notifications"
TELEGRAM_BOT_TOKEN          # required
FCM_PROJECT_ID              # required (even though PushChannel is currently disabled)
FCM_SERVICE_ACCOUNT_JSON    # required (even though PushChannel is currently disabled)
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
