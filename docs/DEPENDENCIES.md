# event-notifier Dependencies

## Depends On

### RabbitMQ

| Property | Value |
|----------|-------|
| Role | Message ingress (domain events) |
| Protocol | AMQP 0-9-1 |
| Config | `RABBIT_URL`, `RABBIT_EXCHANGE`, `NOTIFICATIONS_QUEUE` |
| Client | FastStream `RabbitBroker` |
| Connection lifetime | App-scoped (created in lifespan, closed on shutdown) |

**Failure modes:**
- **Connection refused at startup:** Service fails to start (FastStream broker.start() raises).
- **Connection lost at runtime:** FastStream handles reconnection internally. Messages may be redelivered after reconnect.
- **Queue does not exist:** Consumer declares queue on startup (`declare=True` with DLQ args). If exchange does not exist, binding fails.

Reference: `ioc.py:42-47`, `adapters/consumer.py:33-39`

---

### PostgreSQL (own database)

| Property | Value |
|----------|-------|
| Role | Routing rules, notification outbox, idempotency tracking |
| Protocol | PostgreSQL wire protocol via asyncpg |
| Config | `DATABASE_URL` |
| Client | `asyncpg.Pool` (min_size=2, max_size=10) |
| Connection lifetime | App-scoped pool |
| Schema management | Auto-created on startup (`db/schema.py:65-69`) |

**Tables owned:**
- `routing_rules` -- event_type to recipient field/role mapping
- `processed_events` -- idempotency log (cleaned up hourly, 7-day retention)
- `notification_outbox` -- transactional outbox for delivery tasks

**Failure modes:**
- **Connection refused at startup:** Service fails to start (pool creation raises).
- **Connection lost at runtime (transient):** asyncpg pool reconnects transparently for subsequent `acquire()` calls. In-flight transactions fail and propagate to caller.
- **Pool exhaustion:** `acquire()` blocks until a connection is available (no explicit timeout configured). Under sustained load with slow queries, this can stall the outbox sender and consumer concurrently.
- **Deadlocks:** Unlikely given simple single-table operations. `FOR UPDATE SKIP LOCKED` avoids inter-process contention on outbox rows.

Reference: `ioc.py:32-35`, `db/repository.py:14-16`

---

### event-users (HTTP service)

| Property | Value |
|----------|-------|
| Role | Resolve user UUID to notification contacts (email, telegram_chat_id) |
| Protocol | HTTP REST |
| Endpoint | `GET /users/{user_id}` |
| Auth | Bearer token (`EVENT_USERS_TOKEN`) |
| Config | `EVENT_USERS_URL`, `EVENT_USERS_TOKEN` |
| Client | httpx `AsyncClient` (connect=5s, read=15s, write=5s) |
| Connection lifetime | App-scoped |

**Request/Response:**
- Request: `GET /users/{user_id}` with `Authorization: Bearer <token>`
- Response 200: `{"email": "...", "telegram_chat_id": "...", ...}`
- Response 404: user not found

**Failure modes:**
- **404 (user not found):** Returns empty contact list. Recipient skipped with warning log. No retry.
- **5xx / timeout / connection error:** Exception raised, propagates to consumer, message nacked/requeued by FastStream. This is intentional -- transient errors trigger redelivery.
- **Partial data (email present, telegram_chat_id missing):** Only email channel contact returned. Notification sent via available channels only.
- **event-users fully down for extended period:** Messages accumulate in RabbitMQ queue. If queue reaches memory/length limits, RabbitMQ may apply flow control or reject publishes upstream.

Reference: `infrastructure/users_client.py:71-121`, `ioc.py:50-55`

---

### UniSender Go API (email delivery)

| Property | Value |
|----------|-------|
| Role | Send transactional emails |
| Protocol | HTTPS |
| Endpoint | `POST /ru/transactional/api/v1/email/send.json` |
| Base URL | `https://go.unisender.ru` |
| Auth | API key in request body (`api_key` field) |
| Config | `UNISENDER_API_KEY`, `UNISENDER_FROM_EMAIL`, `UNISENDER_FROM_NAME` |
| Client | httpx `AsyncClient` (connect=5s, read=15s, write=5s) |

**Failure modes:**
- **HTTP 4xx/5xx:** Returns `DeliveryResult(success=False)`. Outbox record retried with exponential backoff.
- **Timeout (>15s read):** httpx raises `TimeoutException`. Caught by OutboxSender, treated as failure, retried.
- **Invalid API key:** Persistent 401/403. Record retries until max_retries exhausted, then marked `failed`.
- **Rate limiting:** UniSender returns 429. Treated as failure, retried with backoff (backoff timing may not align with rate limit window).

Reference: `infrastructure/channels/email.py:22-78`, `ioc.py:58-68`

---

### Telegram Bot API

| Property | Value |
|----------|-------|
| Role | Send Telegram chat messages |
| Protocol | HTTPS |
| Endpoint | `POST /bot{token}/sendMessage` |
| Base URL | `https://api.telegram.org` |
| Auth | Bot token embedded in URL path |
| Config | `TELEGRAM_BOT_TOKEN` |
| Client | httpx `AsyncClient` (connect=5s, read=15s, write=5s) |

**Failure modes:**
- **HTTP 403 (Forbidden):** User blocked the bot. Returns `DeliveryResult(success=False)`. Will retry and eventually fail permanently.
- **HTTP 429 (Too Many Requests):** Rate limited. Retried with exponential backoff.
- **HTTP 5xx:** Telegram server error. Retried.
- **Timeout:** Treated as failure, retried.
- **Invalid bot token:** Persistent 401. All Telegram sends fail until token is corrected.
- **chat_id invalid:** 400 error. Retried until max_retries, then marked failed.

Reference: `infrastructure/channels/telegram.py:22-52`, `ioc.py:70-76`

---

### FCM HTTP v1 API (DISABLED)

| Property | Value |
|----------|-------|
| Role | Push notifications to mobile devices |
| Protocol | HTTPS |
| Endpoint | `POST /v1/projects/{project_id}/messages:send` |
| Base URL | `https://fcm.googleapis.com` |
| Auth | OAuth2 Bearer token via `IAccessTokenProvider` |
| Config | `FCM_PROJECT_ID` (optional), `FCM_SERVICE_ACCOUNT_JSON` (optional) |
| Status | **Not wired in DI. Channel adapter exists but is not registered in OutboxSender.** |

Reference: `infrastructure/channels/push.py:26-72`, `ioc.py:99` (commented out)

---

## Provides To

### End users (notifications)

| Channel | Recipient |
|---------|-----------|
| Email | Users with email addresses (via UniSender templates) |
| Telegram | Users with linked Telegram accounts (via bot messages) |

The service does not expose any HTTP API beyond a `/health` endpoint.
It does not publish delivery result events (gap -- see API_CONTRACTS.md).

---

## Dependency Failure Impact Matrix

| Dependency | Impact on service | Recovery |
|-----------|-------------------|----------|
| RabbitMQ down | No new events consumed; outbox sender continues processing existing records | Automatic on reconnect |
| PostgreSQL down | Consumer cannot check idempotency or write outbox; outbox sender cannot fetch/update records; all operations fail | Automatic on pool reconnect; messages requeued |
| event-users down | Consumer nacks messages (requeued); no new outbox records written | Automatic on recovery; backlog drains from queue |
| UniSender down | Email outbox records enter retry loop (10s, 40s, 90s, 160s, 250s); marked failed after 5 retries | Manual: records stuck in `failed` status require re-processing or manual intervention |
| Telegram API down | Same as UniSender: retry with backoff, then fail | Same as UniSender |
| All deps healthy | Normal operation: ~1s latency from event to outbox write; delivery latency depends on channel API | -- |
