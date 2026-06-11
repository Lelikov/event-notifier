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
(status: pending/processing/delivered/failed, enforced by CHECK constraint).

## Channels

| Channel | Provider | Template mechanism |
|---------|----------|--------------------|
| Email | UniSender Go transactional API | `UNISENDER_TEMPLATE_IDS` config maps locale -> TriggerEvent value -> template UUID (flat legacy form = default locale); flat scalar `global_substitutions` |
| Telegram | Telegram Bot API `/sendMessage` | Jinja2 file per locale and trigger: `event_notifier/templates/<locale>/telegram/<TRIGGER_EVENT>.j2` (ru + en shipped) |
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

## Environment Variables

```
RABBIT_URL                  # default amqp://guest:guest@localhost:5672/
RABBIT_EXCHANGE             # default "events"; queue name/args fixed by event_schemas.queues
CONSUMER_PREFETCH_COUNT     # per-channel QoS, default 10
GRACEFUL_TIMEOUT            # broker graceful shutdown, default 30s
DATABASE_URL                # postgresql+asyncpg:// (required)
EVENT_USERS_URL             # required
EVENT_USERS_TOKEN           # required (Bearer)
EVENTS_ENDPOINT_URL         # optional; unset disables delivery-result publishing
EVENTS_API_KEY              # auth for EVENTS_ENDPOINT_URL
UNISENDER_API_KEY           # required (sent as X-API-KEY header, never in body)
UNISENDER_FROM_EMAIL        # required
UNISENDER_FROM_NAME         # default "Notifications"
UNISENDER_TEMPLATE_IDS      # JSON dict: locale -> {TriggerEvent value -> UniSender template UUID}
                            # (legacy flat {TriggerEvent: UUID} form = default locale)
DEFAULT_LOCALE              # default template language, default "ru"
TELEGRAM_BOT_TOKEN          # required
FCM_PROJECT_ID              # optional (PushChannel not registered)
FCM_SERVICE_ACCOUNT_JSON    # optional
DEBUG / LOG_LEVEL           # logging
```

## Known Limitations

1. **PushChannel not registered** — code and retry classification ready; needs FCM
   credentials, an IoC provider and an access-token provider implementation.
2. **Operator redrive of `failed` rows is manual SQL** —
   `UPDATE notification_outbox SET status='pending', retry_count=0 WHERE status='failed' AND ...`.
3. **No metrics** — observability is structured logs only.
