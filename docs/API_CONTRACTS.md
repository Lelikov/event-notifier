# event-notifier API Contracts

## RabbitMQ Consumed

### Queue

| Property | Value |
|----------|-------|
| Queue name | `events.notification.commands` |
| Exchange | `events` (topic) |
| Routing key | `events.notification.commands` |
| Durable | yes |
| DLQ | `events.dlx` exchange (x-dead-letter-exchange argument) |
| Declare | `True` (queue declared by consumer on startup) |

Reference: `adapters/consumer.py:33-39`, `config.py:19`

### CloudEvent Format (binary mode)

Messages arrive as binary-mode CloudEvents. Headers carry CE attributes; body is JSON.

**Required headers:**

| Header | Example | Source |
|--------|---------|--------|
| `ce-type` | `booking.created` | Must match a key in `DOMAIN_EVENT_TO_TRIGGER` |
| `ce-id` | `"uuid-string"` | Used for idempotency (`processed_events` table) |
| `ce-source` | `"event-receiver"` | Stored in DomainEvent.source |
| `ce-specversion` | `"1.0"` | CloudEvents spec |

**Optional headers:**

| Header | Fallback |
|--------|----------|
| `ce-booking_id` | Falls back to `data.booking_id` if absent (`consumer.py:69`) |

### Accepted event types

Only events whose `ce-type` matches `DOMAIN_EVENT_TO_TRIGGER` are processed.
Unknown types are logged at WARNING level and the message is ACKed (skipped).

Reference: `event_types.py:9-15`

```
booking.created
booking.cancelled
booking.rescheduled
booking.reassigned
booking.reminder_sent
```

### Payload Schema (JSON body)

The body is parsed as `DomainEvent.data` (`dict[str, Any]`). The routing rules
extract recipient UUIDs from this payload using dot-notation field paths.

**Expected fields (based on seeded routing rules in `db/schema.py:50-61`):**

```json
{
  "booking_id": "uuid-string",
  "volunteer_id": "uuid-string",
  "client_id": "uuid-string",
  // ... additional booking context passed as template_data to channels
}
```

The entire `data` dict is stored as `template_context` in the outbox and passed
to channel `send()` as `template_data`.

### Routing Rules (DB-driven)

Recipients are resolved dynamically from the `routing_rules` table:

| event_type | recipient_field | recipient_role |
|-----------|-----------------|----------------|
| `booking.created` | `volunteer_id` | `volunteer` |
| `booking.created` | `client_id` | `client` |
| `booking.cancelled` | `volunteer_id` | `volunteer` |
| `booking.cancelled` | `client_id` | `client` |
| `booking.rescheduled` | `volunteer_id` | `volunteer` |
| `booking.rescheduled` | `client_id` | `client` |
| `booking.reassigned` | `volunteer_id` | `volunteer` |
| `booking.reassigned` | `client_id` | `client` |
| `booking.reminder_sent` | `client_id` | `client` |

Reference: `db/schema.py:49-61`

New routing rules can be added to the DB without code changes. The `recipient_field`
supports dot-notation for nested paths (e.g., `"user.id"`).
Reference: `domain/services/routing.py:6-19`

---

## Outbox Processing

Each outbox record (`notification_outbox` table) goes through this lifecycle:

```
pending --> [fetch_pending_outbox: status='processing'] --> channel.send()
                |                                               |
                |                                  success?     |
                |                                 /       \     |
                |                               yes        no   |
                |                                |          |   |
                v                                v          v   |
           (processing)                   delivered    retry/failed
```

### Per-record processing (`adapters/outbox_sender.py:55-119`):

1. Parse `channel` string into `ChannelType` enum.
2. Look up channel adapter from `channels` dict.
3. Map `event_type` to `trigger_event` via `DOMAIN_EVENT_TO_TRIGGER` (falls back to raw event_type string).
4. Construct `ChannelContact` from outbox record fields.
5. Call `channel.send(contact=..., trigger_event=..., template_data=record.template_context)`.
6. On success: `mark_delivered(record.id)`.
7. On failure: increment retry_count; if `retry_count > max_retries` (default 5), `mark_failed`; otherwise `mark_retry` with exponential backoff delay.

### Retry backoff formula

`delay_seconds = 10 * retry_count^2`

| Retry # | Delay |
|---------|-------|
| 1 | 10s |
| 2 | 40s |
| 3 | 90s |
| 4 | 160s |
| 5 | 250s |

Reference: `adapters/outbox_sender.py:15-17`

### Batch processing

- Batch size: 10 (configurable via constructor)
- Poll interval: 1.0 second (configurable via constructor)
- Selection: `WHERE status = 'pending' AND scheduled_at <= NOW() ORDER BY scheduled_at`
- Locking: `FOR UPDATE SKIP LOCKED` inside a transaction (prevents duplicate pickup)

Reference: `db/repository.py:75-110`

---

## RabbitMQ NOT Published (delivery results -- UNIMPLEMENTED)

**This is a documented gap.** The architecture contract specifies that after
successful delivery, the service publishes result events back to event-receiver:

| Intended event type | Constant | Defined in |
|---------------------|----------|-----------|
| `notification.email.message_sent` | `NOTIFICATION_EMAIL_SENT` | `event_types.py:3` |
| `notification.telegram.message_sent` | `NOTIFICATION_TELEGRAM_SENT` | `event_types.py:4` |
| `notification.push.message_sent` | `NOTIFICATION_PUSH_SENT` | `event_types.py:5` |

**Current state:** No publisher implementation exists. `OutboxSender._process_record`
has a TODO comment at line 92-93 acknowledging this gap. Downstream consumers of
`events.notification.delivery` queue will receive nothing.

**Impact:** Any service expecting delivery confirmations (audit logs, user-facing
delivery status) will not function until this is implemented.

---

## Error Handling

### Unknown event type in consumer

- **Behavior:** Message is ACKed and skipped. Warning logged.
- **Reference:** `adapters/consumer.py:65-67`

### Malformed CloudEvent (unparseable headers/body)

- **Behavior:** Exception raised from `from_http()`, propagates to FastStream.
  With DLQ binding (`x-dead-letter-exchange: events.dlx`), the message is
  dead-lettered after rejection.
- **Reference:** `adapters/consumer.py:58-62`

### Missing contacts (user not found -- 404)

- **Behavior:** `get_contacts_by_id` returns empty list. The user is skipped
  with a warning log. If ALL recipients resolve to empty, the event is NOT
  marked as processed and the message is ACKed (event effectively dropped
  for that attempt but idempotency allows re-processing if re-published).
- **Reference:** `infrastructure/users_client.py:86-89`, `application/use_cases/process_domain_event.py:67-73`

### event-users service unavailable (5xx / timeout / connection error)

- **Behavior:** Exception is raised from `get_contacts_by_id`, propagates
  through the use case and consumer to FastStream, causing message nack/requeue.
- **Reference:** `infrastructure/users_client.py:90-94`

### Channel send failure (HTTP error from UniSender/Telegram/FCM)

- **Behavior:** Channel returns `DeliveryResult(success=False, error=...)`.
  OutboxSender increments retry_count and schedules retry with exponential backoff.
  After `max_retries` (default 5) exhausted, record is marked `failed`.
- **Reference:** `adapters/outbox_sender.py:90-119`

### Channel send raises unexpected exception

- **Behavior:** Caught by OutboxSender's try/except. Treated same as
  `success=False` -- triggers retry logic.
- **Reference:** `adapters/outbox_sender.py:82-86`

### Unknown channel type in outbox record

- **Behavior:** Record is immediately marked `failed` (no retry).
- **Reference:** `adapters/outbox_sender.py:56-59`

### No channel adapter registered for known channel type

- **Behavior:** Record is immediately marked `failed` (no retry).
- **Reference:** `adapters/outbox_sender.py:62-64`

### Unknown trigger_event in Email channel

- **Behavior:** Returns `DeliveryResult(success=False)` with error message.
  Record enters retry loop (will fail all retries and eventually be marked failed).
- **Reference:** `infrastructure/channels/email.py:46-51`

### Unknown trigger_event in Telegram channel

- **Behavior:** Falls back to generic message string "notification: {trigger_event}"
  and sends it (returns success=True). This is a known design inconsistency.
- **Reference:** `infrastructure/channels/telegram.py:34`
