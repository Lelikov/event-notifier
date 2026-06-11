# event-notifier Dependencies

## Depends On

### RabbitMQ

| Property | Value |
|----------|-------|
| Role | Ingress: `notification.send_requested` commands |
| Protocol | AMQP 0-9-1 (FastStream `RabbitBroker`, `graceful_timeout` from config) |
| Config | `RABBIT_URL`, `RABBIT_EXCHANGE`; queue spec from `event_schemas.queues` |
| Topology | Declares `events.notification.commands` (canonical args) + `events.dlx` / `events.notification.commands.dlq` idempotently at startup |

**Failure modes:**
- Connection refused at startup → `broker.start()` raises, service does not start.
- Connection lost at runtime → FastStream reconnects; unacked messages are redelivered
  (processing is idempotent via `processed_events`).
- Inequivalent queue arguments declared elsewhere → PRECONDITION_FAILED; every declarer
  must use `event_schemas.queues` verbatim.

---

### PostgreSQL (own database)

| Property | Value |
|----------|-------|
| Role | Idempotency log + transactional outbox |
| Client | SQLAlchemy async engine (`asyncpg` driver), pool_size=10, max_overflow=20, pre-ping |
| Sessions | Fresh `AsyncSession` per operation (`SqlExecutor`); no shared session state |
| Schema | Alembic migrations (`uv run alembic upgrade head`) — no auto-create at startup |

**Tables owned:** `processed_events` (hourly cleanup, 7-day retention), `notification_outbox`.

**Failure modes:**
- Transient outage during consume → classified transient → in-process backoff, then
  NACK(requeue) — nothing lost.
- Crash mid-delivery → row stays `processing`, reaped back to `pending` after 300 s.
- Down → `/health` reports `database: false` and returns 503.

---

### event-users (HTTP)

| Property | Value |
|----------|-------|
| Endpoint | `GET {EVENT_USERS_URL}/api/users/id/{user_id}` |
| Auth | `Authorization: Bearer {EVENT_USERS_TOKEN}` |
| Timeouts | connect 5s / read 15s / write 5s / pool 5s |

**Failure modes:**
- **404 (user gone):** recipient degrades to email-only — the email address comes from
  the command itself, so the delivery is never lost to a missing profile.
- **5xx / auth / transport / timeout:** `UsersServiceError` → message NACKed and
  retried; never ACKed away. Extended outage = backlog in RabbitMQ.

---

### event-receiver (HTTP, optional)

| Property | Value |
|----------|-------|
| Endpoint | `POST {EVENTS_ENDPOINT_URL}` — binary-mode CloudEvent ingest |
| Auth | `Authorization: {EVENTS_API_KEY}` |
| Purpose | `notification.*.message_sent` delivery results |

**Failure modes:** fire-and-forget — publish failure is logged and swallowed (the
notification itself is already delivered); deterministic `ce-id` (UUIDv5 of the outbox
record) deduplicates re-publishes. Unset URL disables publishing (warning at startup).

---

### UniSender Go (email)

| Property | Value |
|----------|-------|
| Endpoint | `POST https://go.unisender.ru/ru/transactional/api/v1/email/send.json` |
| Auth | `X-API-KEY` header (never in body, never logged) |
| Templates | UUIDs from `UNISENDER_TEMPLATE_IDS[trigger_event]`; flat scalar `global_substitutions` |

**Failure modes:**
- 408/429/5xx/transport → transient → outbox retry, capped exponential backoff (10s
  doubling, cap 30 min, max 10 retries).
- Other 4xx (bad API key, bad payload) and missing template id → permanent `failed`
  immediately — no provider rate-limit burn.

---

### Telegram Bot API

| Property | Value |
|----------|-------|
| Endpoint | `POST https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage` |
| Body | `{"chat_id", "text", "parse_mode": "HTML"}` rendered from `templates/telegram/*.j2` |

**Failure modes:** same classification as email (403 blocked-bot and 400 bad chat_id are
permanent; 429/5xx transient). Recipients without a telegram contact in event-users
simply get no telegram outbox record.

---

### FCM HTTP v1 (optional, NOT registered)

`PushChannel` is implemented with the same retryable classification but not wired in
`ioc.py`. `FCM_PROJECT_ID` / `FCM_SERVICE_ACCOUNT_JSON` are optional and unused at runtime.

---

## Provides To

- **End users:** email (UniSender templates) and Telegram messages.
- **event-receiver:** `notification.{email,telegram,push}.message_sent` delivery-result
  CloudEvents (routed onward to `events.notification.delivery`).
- HTTP surface: `/health` only.

---

## Dependency Failure Impact Matrix

| Dependency | Impact | Recovery |
|-----------|--------|----------|
| RabbitMQ down | No new commands consumed; outbox sender keeps draining existing records | Automatic on reconnect |
| PostgreSQL down | Consume attempts NACK/requeue; outbox loop logs errors and continues polling | Automatic; reaper recovers `processing` rows |
| event-users down | Commands retried in-process then requeued; backlog in queue | Automatic; backlog drains |
| UniSender / Telegram down | Outbox retries with capped backoff up to ~hours, then `failed` | Automatic within budget; operator SQL redrive after |
| event-receiver down | Delivery results dropped (logged); deliveries unaffected | Manual re-publish not needed (results are advisory) |
