# event-notifier Service Overview

> **Maturity: EARLY / PRE-PRODUCTION**
>
> This service has passed a full audit and critical fixes have been applied (queue
> name alignment, FCM fields made optional, FOR UPDATE wrapped in a transaction,
> contact resolution now raises on transient errors, DLQ binding added, explicit
> HTTP timeouts, processed_events cleanup task). However, remaining gaps exist --
> see "Known Limitations" at the bottom of this document.

## Domain

Notification fan-out dispatcher. Receives domain events from the booking
lifecycle, resolves recipients via routing rules and contact lookups, writes
delivery tasks to a transactional outbox, and asynchronously delivers through
multiple channels.

## Request Flow

```
RabbitMQ topic exchange ("events")
  queue: events.notification.commands
  binding key: events.notification.commands
        |
        v
NotificationConsumer                       (adapters/consumer.py:14-88)
  - parses CloudEvent (cloudevents-sdk from_http)
  - filters by DOMAIN_EVENT_TO_TRIGGER map (event_types.py:9-15)
  - constructs DomainEvent frozen dataclass
        |
        v
ProcessDomainEventUseCase                  (application/use_cases/process_domain_event.py:15-102)
  - idempotency check via processed_events table
  - loads routing_rules from DB for event_type
  - apply_routing_rules extracts (user_id, role) from event data (domain/services/routing.py:22-40)
  - per recipient: UsersClient.get_contacts_by_id() -> list[ChannelContact]
  - writes all outbox records + marks processed_events in one DB transaction
        |
        v
notification_outbox table (PostgreSQL)
        |
        v (poll every 1 s)
OutboxSender                               (adapters/outbox_sender.py:20-127)
  - fetch_pending_outbox (SELECT ... FOR UPDATE SKIP LOCKED inside txn)
  - per record: resolve channel adapter, call channel.send()
  - on success: mark_delivered
  - on failure: mark_retry (exponential backoff) or mark_failed after max_retries
        |
        +---> EmailChannel    (infrastructure/channels/email.py)
        +---> TelegramChannel (infrastructure/channels/telegram.py)
        +---> (PushChannel    -- wired but disabled; FCM credentials optional)
```

## Database

Schema is managed by **Alembic** with async migrations. ORM models (`db/models.py`) exist only for
Alembic autogenerate — all queries use raw SQL via `SqlExecutor` (`adapters/sql.py`), a thin
wrapper over SQLAlchemy `AsyncSession` with `text()` queries (same pattern as event-saver).

Tables: `routing_rules`, `processed_events`, `notification_outbox`.

## Channels

| Channel | Provider | Template mechanism | Reference |
|---------|----------|--------------------|-----------|
| Email | UniSender Go transactional API | `_TEMPLATE_MAP` maps `TriggerEvent` enum → UniSender template_id | `infrastructure/channels/email.py` |
| Telegram | Telegram Bot API `/sendMessage` | `_MESSAGE_TEMPLATES` maps `TriggerEvent` enum → Russian strings | `infrastructure/channels/telegram.py` |
| Push (disabled) | FCM HTTP v1 | `_PUSH_TITLES` maps `TriggerEvent` enum → title strings | `infrastructure/channels/push.py` |

Template map keys use `TriggerEvent` enum from `event-schemas` (not raw strings).
Recipient roles use `RecipientRole` convention from `event-schemas`: `"organizer"` and `"client"`.

## Template Mapping

The consumer maps CloudEvent `type` to a `TriggerEvent` enum via `DOMAIN_EVENT_TO_TRIGGER` (`event_types.py`):

| CloudEvent type | trigger_event |
|-----------------|---------------|
| `booking.created` | `BOOKING_CREATED` |
| `booking.cancelled` | `BOOKING_CANCELLED` |
| `booking.rescheduled` | `BOOKING_RESCHEDULED` |
| `booking.reassigned` | `BOOKING_REASSIGNED` |
| `booking.reminder_sent` | `BOOKING_REMINDER` |

Each channel independently maps the trigger_event to a provider-specific template/message.
Email and Telegram templates also contain `BOOKING_REJECTED` which is currently unreachable
(no routing rule or DOMAIN_EVENT_TO_TRIGGER entry for `booking.rejected`).

## Adding a New Channel (step-by-step)

1. **Define the ChannelType** -- add an enum value to `ChannelType` in `domain/models/notification.py:8-11` (if not already present).
2. **Implement the channel** -- create `infrastructure/channels/<name>.py` implementing `INotificationChannel` protocol (`interfaces/channels.py`). The `send()` method receives a `ChannelContact`, `trigger_event: TriggerEvent`, and `template_data` dict; returns a `DeliveryResult`.
3. **Register in DI** -- add a `provide_<name>_channel` method in `ioc.py` and inject it into `provide_outbox_sender`'s `channels` dict.
4. **Update UsersClient** -- ensure `get_contacts_by_id` (`infrastructure/users_client.py:71-121`) returns `ChannelContact` entries for the new channel type (may require extending event-users' response).
5. **Add templates** -- populate the channel's internal template map for each trigger_event string.
6. **Update event_types.py** -- add a `NOTIFICATION_<CHANNEL>_SENT` constant for delivery result events (when publishing is implemented).

## Runtime Dependencies

| Dependency | Purpose | Config var |
|-----------|---------|------------|
| RabbitMQ | Consume domain events | `RABBIT_URL`, `RABBIT_EXCHANGE` (queue spec from `event_schemas.queues`) |
| PostgreSQL | Routing rules, outbox, idempotency tracking | `DATABASE_URL` |
| event-users (HTTP) | Resolve user contacts by UUID | `EVENT_USERS_URL`, `EVENT_USERS_TOKEN` |
| UniSender Go API | Send transactional email | `UNISENDER_API_KEY`, `UNISENDER_FROM_EMAIL`, `UNISENDER_FROM_NAME` |
| Telegram Bot API | Send Telegram messages | `TELEGRAM_BOT_TOKEN` |
| FCM (optional) | Push notifications (disabled) | `FCM_PROJECT_ID`, `FCM_SERVICE_ACCOUNT_JSON` |

## Environment Variables

```
RABBIT_URL              # amqp://... (default: amqp://guest:guest@localhost:5672/)
RABBIT_EXCHANGE         # topic exchange name (default: "events")
DATABASE_URL            # PostgreSQL DSN (required, no default)
EVENT_USERS_URL         # base URL of event-users service (required)
EVENT_USERS_TOKEN       # Bearer token for event-users API (required)
UNISENDER_API_KEY       # UniSender Go API key (required)
UNISENDER_FROM_EMAIL    # sender email address (required)
UNISENDER_FROM_NAME     # sender display name (default: "Notifications")
TELEGRAM_BOT_TOKEN      # Telegram bot token (required)
FCM_PROJECT_ID          # Firebase project ID (optional, None)
FCM_SERVICE_ACCOUNT_JSON# Firebase service account JSON (optional, None)
DEBUG                   # enable console log rendering (default: false)
LOG_LEVEL               # structlog level (default: "INFO")
```

Reference: `config.py:1-33`

## Known Limitations / Remaining Production Readiness Gaps

1. **Delivery result events NOT published** -- `event_types.py` defines `NOTIFICATION_EMAIL_SENT` / `NOTIFICATION_TELEGRAM_SENT` / `NOTIFICATION_PUSH_SENT` constants, and event-receiver has routing rules for `events.notification.delivery` queue, but `OutboxSender` never publishes these events after delivery. See TODO at `adapters/outbox_sender.py:92-93`.

2. **No consumer-level integration tests** -- `NotificationConsumer` (`adapters/consumer.py`) has zero test coverage. CloudEvent parsing logic and event type filtering are untested.

3. **Telegram fallback leaks trigger_event** -- unknown trigger_event strings produce a user-visible "notification: BOOKING_REJECTED" message instead of failing cleanly (`infrastructure/channels/telegram.py:34`).

4. **Outbox polling has no backoff** -- polls DB every 1 second even when empty (`adapters/outbox_sender.py:48`). Under quiet periods this is unnecessary load.

5. **`BOOKING_REJECTED` template unreachable** -- email and telegram channels define templates for this trigger, but no routing rule or DOMAIN_EVENT_TO_TRIGGER entry exists.

6. **`get_contacts_by_email` is dead code** -- method exists in `UsersClient` and interface but is never called by `ProcessDomainEventUseCase`.

7. **NOTIFICATION_SERVICE_ARCHITECTURE.md is stale** -- describes a completely different design (meeting.* events, Jinja2, WhatsApp, aiohttp). Should be archived.
