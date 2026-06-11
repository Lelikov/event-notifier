# event-notifier API Contracts

## RabbitMQ Consumed

### Queue (canonical spec: `event_schemas.queues.NOTIFICATION_COMMANDS_QUEUE`)

| Property | Value |
|----------|-------|
| Queue name | `events.notification.commands` |
| Exchange | `events` (topic, durable) |
| Routing key | `events.notification.commands` |
| Arguments | `x-max-priority=10`, `x-dead-letter-exchange=events.dlx`, `x-dead-letter-routing-key=events.notification.commands.dlq` |
| DLQ | `events.notification.commands.dlq` bound to `events.dlx`, `x-message-ttl=24h` — declared idempotently by this service at startup |
| QoS | per-channel `prefetch_count` (`CONSUMER_PREFETCH_COUNT`, default 10) |

All declarers MUST use the spec verbatim or RabbitMQ rejects with PRECONDITION_FAILED.

### CloudEvent (binary mode)

| Header | Example | Notes |
|--------|---------|-------|
| `ce-type` | `notification.send_requested` | the ONLY processed type; others are ACKed with a warning |
| `ce-id` | uuid string | idempotency key (`processed_events`) |
| `ce-source` | `event-receiver` | |
| `ce-specversion` | `1.0` | |
| `ce-bookingid` | uuid string | optional; falls back to `original.booking_id` |

### Body: canonical envelope (`event_schemas.envelope.EventEnvelope`)

```json
{
  "original": {
    "booking_id": "…",
    "trigger_event": "BOOKING_CREATED",
    "recipients": [
      {"email": "org@example.com", "role": "organizer"},
      {"email": "cli@example.com", "role": "client"}
    ],
    "template_data": {"title": "…", "start_time": "2026-06-12T10:00:00Z", "...": "..."}
  },
  "normalized": {
    "participants": [
      {"email": "org@example.com", "role": "organizer", "user_id": "<event-users uuid>", "time_zone": "Europe/Moscow"}
    ]
  }
}
```

- `original` is validated as `NotificationCommandPayload` (`event_schemas.notification`);
  validation failure = poison = DLQ.
- `template_context` given to channels = `original` merged with `template_data`
  (template_data wins; decision D6), then localized per recipient
  (`start_time_local`, `end_time_local`, `time_zone` added when the recipient's zone is known).
- `user_id`/`time_zone` are matched to recipients by lowercased email.

### Ack policy

| Condition | Action |
|-----------|--------|
| Unparseable CloudEvent / envelope / payload | `RejectMessage` → DLQ |
| Unknown `ce-type` | ACK + warning |
| Transient failure (event-users 5xx/transport, DB connectivity) | 3 in-process attempts with exponential backoff, then `NackMessage(requeue=True)` |
| Any other exception from the use case | `RejectMessage` → DLQ |
| Duplicate `ce-id` | ACK (idempotent no-op) |
| Zero deliverable contacts | ACK, event claimed, structured `no_contacts` warning |

---

## Outbox Processing

```
pending --claim batch (FOR UPDATE SKIP LOCKED -> status='processing')--> channel.send()
   ^            |                                       |
   |       reaper (stale >300s,                 success | failure
   |       +1 retry_count)                        |     |
   |            |                                 v     v
   +------------+                          delivered   retryable? --no--> failed
   |                                                      |yes
   +---- mark_retry (scheduled_at += delay) --------------+   (retry_count > max_retries -> failed)
```

- Retry delay: `min(10 * 2^(retry-1), 1800)` seconds; `max_retries` default 10
  (total window ≈ several hours).
- Permanent = non-retryable `DeliveryResult` (4xx except 408/429, missing template),
  unknown channel, unknown trigger_event.
- Poll: 1 s, exponential idle backoff to 30 s; batch size 10.
- `failed` is terminal; operator redrive:
  `UPDATE notification_outbox SET status='pending', retry_count=0 WHERE status='failed' AND …`.

---

## Delivery Results Published (HTTP → event-receiver)

After each successful delivery `DeliveryResultPublisher` POSTs a binary-mode
CloudEvent to `EVENTS_ENDPOINT_URL` (disabled when unset; `EVENTS_API_KEY` goes
into the `Authorization` header as `Bearer {EVENTS_API_KEY}`):

| Channel | `ce-type` | Extra payload fields |
|---------|-----------|----------------------|
| email | `notification.email.message_sent` | `job_id` (UniSender) |
| telegram | `notification.telegram.message_sent` | — |
| push | `notification.push.message_sent` | `device_token`, `message_id` |

Common payload: `{"email", "recipient_role", "trigger_event", "booking_uid"}`.
`ce-source=event-notifier`; `ce-id` is a deterministic UUIDv5 of the outbox record id
(re-publishes deduplicate downstream). Publishing is fire-and-forget: failures are
logged and never retried (the notification itself is already delivered).

---

## External Provider Contracts (hard invariants)

### UniSender Go
- `POST https://go.unisender.ru/ru/transactional/api/v1/email/send.json`
- API key in `X-API-KEY` header (never in body, never logged)
- `message.template_id` = real template UUID from `UNISENDER_TEMPLATE_IDS[locale][trigger_event]`
  (recipient locale from `template_context["locale"]`, fallback `DEFAULT_LOCALE`)
- `message.global_substitutions` = flat scalar key/values only (nested structures dropped)

### Telegram Bot API
- `POST https://api.telegram.org/bot{token}/sendMessage`
- `{"chat_id", "text", "parse_mode": "HTML"}`; text rendered from
  `templates/<locale>/telegram/<TRIGGER_EVENT>.j2` (autoescaped; recipient locale with
  fallback to `DEFAULT_LOCALE`, `ru` and `en` sets shipped)

---

## HTTP Endpoints

| Method | Path | Response |
|--------|------|----------|
| GET | `/health` | 200 `{"status":"ok","checks":{...}}` / 503 `{"status":"degraded",...}` — checks: consumer, outbox_sender, database |
