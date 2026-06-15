# event-notifier Service Overview

> **Maturity: PRE-PRODUCTION.** Redesigned in audit-v2 (2026-06-11): the
> DB-driven routing-rules machinery was deleted; the service now executes
> `notification.send_requested` commands whose recipients come from the payload.
> See `docs/AUDIT.md` for the findings → fixes ledger.

## Domain

Notification dispatcher. Consumes `notification.send_requested` CloudEvents from
RabbitMQ, resolves each recipient's delivery channels, writes delivery tasks to a
transactional outbox, delivers them asynchronously via UniSender Go (email) and
Telegram Bot API, and publishes `notification.*.message_sent` delivery-result
events back to event-receiver.

## Request Flow

```
RabbitMQ topic exchange "events"
  queue: events.notification.commands      (spec: event_schemas.queues.NOTIFICATION_COMMANDS_QUEUE)
  DLQ:   events.notification.commands.dlq  (via events.dlx, declared idempotently at startup)
        |
        v
NotificationConsumer                        (adapters/consumer.py)
  - binary CloudEvent -> EventEnvelope {original, normalized}
  - original validated as NotificationCommandPayload (trigger_event, recipients, template_data)
  - recipients merged with receiver-resolved user_id/time_zone from normalized.participants
  - ack policy: poison -> RejectMessage (DLQ); transient -> in-process backoff
    then NackMessage(requeue=True); unknown event type -> ACK + warning
        |
        v
ProcessNotificationCommandUseCase           (application/use_cases/process_notification_command.py)
  - idempotency: processed_events claim + outbox insert in ONE transaction
  - email contact always from the command recipient itself
  - user_id known -> UsersClient GET /api/users/id/{user_id} ADDS telegram channel
    (404 degrades to email-only; transport/5xx raises -> NACK/retry)
  - template_context localized per recipient (start_time_local/end_time_local/time_zone)
  - zero contacts -> explicit "no_contacts" outcome (claimed + structured warning)
        |
        v
notification_outbox (PostgreSQL)
        |  poll (1s, idle-backoff to 30s); stale 'processing' reaped to 'pending' every 60s
        v
OutboxSender                                (adapters/outbox_sender.py)
  - claims batches: UPDATE ... 'processing' WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
  - permanent failure (non-retryable 4xx, missing template, unknown channel/trigger) -> 'failed'
  - transient failure (408/429/5xx/transport) -> capped exponential backoff
    (10s doubling, cap 30 min, max_retries=10)
  - success -> 'delivered' + DeliveryResultPublisher fires notification.*.message_sent
        |
        +--> EmailChannel    UniSender Go send.json (template UUIDs from UNISENDER_TEMPLATE_IDS)
        +--> TelegramChannel Bot API sendMessage (Jinja2: templates/<locale>/telegram/<TRIGGER>.j2)
        +--> (PushChannel    FCM HTTP v1 -- implemented, NOT registered: credentials pending)
```

## Database

Alembic owns the schema (`alembic/versions/`); ORM models (`db/models.py`) exist only
for autogenerate. All queries are raw `text()` SQL via `SqlExecutor` (`adapters/sql.py`),
which opens a fresh `AsyncSession` per operation (safe across concurrent tasks) and
exposes `transaction()` for multi-statement atomic units.

Tables: `processed_events` (idempotency, 7-day TTL cleanup loop), `notification_outbox`
(status: pending/processing/delivered/failed, enforced by CHECK constraint),
`notification_bindings` (admin-managed per-trigger-event channel config — see below).

## Notification Bindings (admin-managed config)

`notification_bindings` — composite PK `(trigger_event, channel)` — stores which channels
are enabled per `TriggerEvent` and the template to use:

| Column | Type | Notes |
|--------|------|-------|
| `trigger_event` | TEXT | `TriggerEvent` value, e.g. `BOOKING_CREATED` |
| `channel` | TEXT | `email` or `telegram` |
| `enabled` | BOOLEAN | controls whether this channel fires for this trigger |
| `unisender_template_id` | TEXT \| NULL | UniSender Go template UUID (email rows) |
| `telegram_body` | TEXT \| NULL | Jinja2 template string rendered at runtime (telegram rows) |
| `updated_at` | TIMESTAMPTZ | set by admin API on each write |

**Seeding (migration `003_notification_bindings`):** the table is populated once at
`alembic upgrade head` from the `UNISENDER_TEMPLATE_IDS` env (default-locale email UUIDs)
and the repo's `templates/<locale>/telegram/<TRIGGER>.j2` files (telegram bodies). After
seeding the DB is authoritative; the `.j2` files serve only as the seed source.

**Runtime reads — `BindingsProvider`:** a short in-memory TTL cache (default 30 s,
`BINDINGS_CACHE_TTL_SECONDS`) avoids per-delivery DB round-trips. `get(trigger_event,
channel)` refreshes from `notification_bindings` on the first call after expiry;
`invalidate()` forces an immediate refresh (called by the admin API after each `PUT`).

**Enablement semantics:** a channel fires for a trigger only when its binding is
`enabled = true` AND the recipient has a contact for that channel (email address or
`telegram_chat_id`). Either condition alone is not sufficient.

**Template rendering:** `EmailChannel` reads `unisender_template_id` from the binding and
passes it to UniSender Go. `TelegramChannel` renders `telegram_body` with Jinja2
`SandboxedEnvironment` (from-string, `autoescape=False`; the sandbox blocks unsafe attribute
access).

## Channels

| Channel | Provider | Template mechanism |
|---------|----------|--------------------|
| Email | UniSender Go transactional API | `unisender_template_id` from `notification_bindings` (runtime); flat scalar `global_substitutions` |
| Telegram | Telegram Bot API `/sendMessage` | `telegram_body` from `notification_bindings`, rendered with Jinja2 `SandboxedEnvironment` |
| Push (not registered) | FCM HTTP v1 | `_PUSH_TITLES` map; enable in `ioc.py` once FCM credentials exist |

All channels classify failures into `DeliveryResult.retryable`:
408/429/5xx/transport -> transient (retry), other 4xx and missing templates -> permanent.
Unknown triggers fail permanently — nothing internal is ever sent to end users.

## Per-Recipient Localization

`normalized.participants[].time_zone` (IANA) is resolved onto each recipient. The use
case adds `start_time_local` / `end_time_local` (recipient zone, `%d.%m.%Y %H:%M`) and
`time_zone` to that recipient's `template_context`; original keys are untouched.
Language: the recipient's locale (producer `recipients[].locale`, fallback
`normalized.participants[].locale`; originally cal.com `language.locale`) is added as
`template_context["locale"]`. Channels pick the template language from it with
fallback to `DEFAULT_LOCALE` (default `ru`).

## Health

`GET /health` returns 200/503 with per-check booleans: consumer started, outbox-sender
task alive, database reachable.

## Admin API

Prefix: `/api/notifications`. Auth: `Authorization: Bearer <NOTIFIER_ADMIN_TOKEN>` (static
service token, compared constant-time; distinct from end-user channels). All endpoints
require the token — 401 otherwise.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/notifications/config` | Return all rows from `notification_bindings` |
| PUT | `/api/notifications/config/{trigger_event}/{channel}` | Upsert a binding (validates Jinja2 for telegram; invalidates the `BindingsProvider` cache) |
| GET | `/api/notifications/unisender-templates` | Cached list from UniSender Go `POST .../template/list.json` (`?refresh=true` to force); in-memory TTL default 1 h (`UNISENDER_TEMPLATE_LIST_TTL_SECONDS`) |
| POST | `/api/notifications/telegram/preview` | Render a Jinja2 `telegram_body` with sample data; validates template syntax |

**PUT body (`BindingIn`):**

```json
{"enabled": true, "unisender_template_id": "<uuid or null>", "telegram_body": "<jinja or null>"}
```

Returns `{"status": "ok"}` on success; 400 with `"detail"` string on invalid Jinja2 or unknown channel.

## Environment Variables

```
RABBIT_URL                      # default amqp://guest:guest@localhost:5672/
RABBIT_EXCHANGE                 # default "events"; queue name/args fixed by event_schemas.queues
CONSUMER_PREFETCH_COUNT         # per-channel QoS, default 10
GRACEFUL_TIMEOUT                # broker graceful shutdown, default 30s
DATABASE_URL                    # postgresql+asyncpg:// (required)
EVENT_USERS_URL                 # required
EVENT_USERS_TOKEN               # required (Bearer)
EVENTS_ENDPOINT_URL             # optional; unset disables delivery-result publishing
EVENTS_API_KEY                  # auth for EVENTS_ENDPOINT_URL
UNISENDER_API_KEY               # required (sent as X-API-KEY header, never in body)
UNISENDER_FROM_EMAIL            # required
UNISENDER_FROM_NAME             # default "Notifications"
UNISENDER_TEMPLATE_IDS          # JSON dict: locale -> {TriggerEvent value -> UniSender template UUID}
                                # (legacy flat {TriggerEvent: UUID} form = default locale)
                                # Used only as the migration-time seed; DB is authoritative at runtime
DEFAULT_LOCALE                  # default template language, default "ru"
TELEGRAM_BOT_TOKEN              # required
NOTIFIER_ADMIN_TOKEN            # required; static service token for /api/notifications/* admin API
BINDINGS_CACHE_TTL_SECONDS      # BindingsProvider in-memory cache TTL, default 30
UNISENDER_TEMPLATE_LIST_TTL_SECONDS  # UniSender template list cache TTL, default 3600
FCM_PROJECT_ID                  # optional (PushChannel not registered)
FCM_SERVICE_ACCOUNT_JSON        # optional
DEBUG / LOG_LEVEL               # logging
```

## Tracing

OpenTelemetry auto-instrumentation (FastAPI, httpx, asyncpg, RabbitMQ via FastStream middleware) + manual spans: `notifier.outbox_claim` (outbox SELECT … FOR UPDATE batch) and `notifier.channel_send` (attribute: `channel` = email/telegram); exported via OTLP/gRPC to the collector → Tempo; gated by `OTEL_SDK_DISABLED` (off by default).

## Known Limitations

1. **PushChannel not registered** — code and retry classification ready; needs FCM
   credentials, an IoC provider and an access-token provider implementation.
2. **Operator redrive of `failed` rows is manual SQL** —
   `UPDATE notification_outbox SET status='pending', retry_count=0 WHERE status='failed' AND ...`.
3. **No metrics** — observability is structured logs only.
4. **Single-locale bindings (v1)** — `notification_bindings` holds one template per
   `trigger_event × channel`; the seeded locale is the `DEFAULT_LOCALE` (default `ru`).
   Per-locale management, a named template library for Telegram, and a push channel
   are out of scope for v1.
