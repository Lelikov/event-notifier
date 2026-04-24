# event-notifier Audit Findings

Audited: 2026-04-20

---

## CRITICAL

---

[CRITICAL] Queue name mismatch: consumer subscribes to wrong queue

Services affected: event-notifier, event-receiver
Location: `event-notifier/event_notifier/config.py:18`, `event-receiver/event_receiver/config.py:85-88`
Description: The config default for `notifications_queue` is `"events.notifications"` (config.py line 18, also visible in `.env.example`). However, event-receiver routes booking domain events (`booking.created`, `booking.cancelled`, etc.) to `"events.notifications"` AND routes `notification.send_requested` CloudEvents to `"events.notification.commands"`. The consumer CLAUDE.md says the queue should be `events.notification.commands`, and the CLAUDE.md env var is named `NOTIFICATION_COMMANDS_QUEUE` — but the actual `config.py` field is `notifications_queue` with a default of `"events.notifications"`. The env var name in `.env.example` is `NOTIFICATIONS_QUEUE`, not `NOTIFICATION_COMMANDS_QUEUE`. The end result is a three-way inconsistency: (1) CLAUDE.md documents the queue as `events.notification.commands`; (2) config.py defaults to `events.notifications`; (3) `.env.example` also uses `events.notifications`. If the service starts without override, it will consume from `events.notifications`, which is NOT the queue event-receiver routes booking domain events to with the newer routing rules — it is a legacy destination. Booking lifecycle events are currently routed to `events.notifications` by event-receiver's old rules, AND ALSO to `events.booking.lifecycle`. The `DOMAIN_EVENT_TO_TRIGGER` map in `event_types.py` maps `booking.created` etc., so the consumer would process them from `events.notifications`. This is consistent only if the intent is to consume the legacy `events.notifications` queue — but it contradicts the CLAUDE.md architecture description entirely. Either the CLAUDE.md is aspirational and the code is the actual runtime state (consuming `events.notifications`), or this is a partially completed migration that left the service misconfigured.
Recommendation: Align the queue name in one authoritative place. If the intent is to consume `events.notification.commands` (as CLAUDE.md states), update `config.py` default to `"events.notification.commands"` and update `.env.example` accordingly. If the intent remains `events.notifications`, update CLAUDE.md. Add a deployment runbook documenting which queue must be pre-created and by which service (since `declare=False`).

---

[CRITICAL] `FOR UPDATE SKIP LOCKED` runs outside a transaction — row lock is immediately released

Services affected: event-notifier
Location: `event-notifier/event_notifier/db/repository.py:75-89`
Description: `fetch_pending_outbox` issues `SELECT ... FOR UPDATE SKIP LOCKED` inside `async with self._pool.acquire() as conn` but without `async with conn.transaction()`. In PostgreSQL, `FOR UPDATE` row-level locks are held only for the duration of the enclosing transaction. asyncpg's `pool.acquire()` by default operates in autocommit mode — there is no explicit transaction. The lock is acquired and immediately released when the statement completes. Between `fetch_pending_outbox` returning and `mark_delivered`/`mark_retry`/`mark_failed` being called, another OutboxSender instance (or a second poll cycle on the same instance) can pick up the same rows. This defeats `SKIP LOCKED`'s purpose entirely and creates a real risk of duplicate deliveries when running multiple instances or when the single instance's poll interval fires again before delivery completes.
Recommendation: Wrap the `fetch_pending_outbox` SELECT in `async with conn.transaction()` and hold the connection (and lock) through the delivery attempt, or use a two-step approach: UPDATE status to `'processing'` in the same transaction as the SELECT, then UPDATE to `'delivered'`/`'failed'` after the send. The latter pattern is more robust for multi-instance deployments.

---

[CRITICAL] FCM env vars required at startup despite PushChannel being disabled

Services affected: event-notifier
Location: `event-notifier/event_notifier/config.py:31-32`
Description: `fcm_project_id` and `fcm_service_account_json` are declared as `Field(strict=True)` with no default value. Pydantic-settings will raise a `ValidationError` at startup if these variables are absent. PushChannel is not wired in `ioc.py` (commented out in `provide_outbox_sender`), so these values are never used at runtime. Any deployment environment that omits FCM credentials (because Push is not in use) will fail to start entirely.
Recommendation: Make FCM fields optional with `Field(default=None)` or give them empty-string defaults. Alternatively, guard them with a feature flag (`push_enabled: bool = False`) and validate FCM fields only when the flag is true. Remove the `strict=True` constraint until FCM is actually enabled.

---

## HIGH

---

[HIGH] No delivery result events published — architecture contract broken

Services affected: event-notifier, event-receiver
Location: `event-notifier/event_notifier/adapters/outbox_sender.py` (entire file), `event-notifier/event_notifier/` (no `publisher.py` exists)
Description: The CLAUDE.md architecture description states that after each channel send the service publishes delivery result events (`notification.email.message_sent`, `notification.telegram.message_sent`, `notification.push.message_sent`) back to event-receiver via `POST /event/cloudevents`. The `event_types.py` defines these constants (`NOTIFICATION_EMAIL_SENT`, `NOTIFICATION_TELEGRAM_SENT`, `NOTIFICATION_PUSH_SENT`). event-receiver has routing rules for all three (`events.notification.delivery` queue). However, no `publisher.py` exists in the codebase and `OutboxSender._process_record` never calls any publisher after delivery — it only calls `mark_delivered`/`mark_retry`/`mark_failed`. The `IResultEventPublisher` interface referenced in CLAUDE.md does not exist. The entire delivery result reporting pipeline is absent.
Recommendation: Implement `ResultEventPublisher` (HTTP POST to event-receiver in CloudEvents binary format) and wire it into `OutboxSender._process_record`. Until then, downstream consumers of `events.notification.delivery` will receive nothing.

---

[HIGH] `processed_events` table has no TTL or cleanup — unbounded growth

Services affected: event-notifier
Location: `event-notifier/event_notifier/db/schema.py:19-22`
Description: `processed_events` stores every processed `cloud_event_id` permanently with no expiry or cleanup. The architecture document mentions "TTL 7 days" but no such mechanism exists in the schema or codebase. Over time this table grows without bound, degrading the `is_processed()` lookup (currently a simple primary-key SELECT, still subject to table bloat and vacuum pressure). There is no cron job, no partitioning, and no `processed_at`-based deletion query anywhere in the code.
Recommendation: Add a scheduled cleanup query (e.g., `DELETE FROM processed_events WHERE processed_at < NOW() - INTERVAL '7 days'`) as either an asyncio background task, a pg_cron job, or a Kubernetes CronJob. Alternatively add a partial index on `processed_at` and an automatic partition by month.

---

[HIGH] `get_contacts_by_id` returns empty list when event-users is unreachable — notification silently dropped

Services affected: event-notifier, event-users
Location: `event-notifier/event_notifier/infrastructure/users_client.py:70-86`, `event-notifier/event_notifier/application/use_cases/process_domain_event.py:66-73`
Description: When `get_contacts_by_id` fails (HTTP error, timeout, or connection refused), it logs a warning and returns `[]`. The use case then logs "No contacts resolved for user, skipping" and continues. If ALL recipients' contact lookups fail (e.g., event-users is down), `outbox_records` will be empty and the event is simply not written to the outbox — but it IS marked as processed in `processed_events` (via `write_outbox_atomically`, which inserts into `processed_events`). Wait — actually: `write_outbox_atomically` is only called when `outbox_records` is non-empty (line 89-91: `if not outbox_records: return`). So when event-users is fully down, the event is NOT marked processed, and RabbitMQ will redeliver (FastStream will nack after the handler raises an exception... except the handler does NOT raise — it returns normally). This means the message is ACKed by FastStream with no notification written and no retry. The event is lost silently.
Recommendation: When `get_contacts_by_id` fails due to infrastructure error (not 404), raise the exception or re-raise after logging so that FastStream can nack the message and it gets requeued (or sent to DLQ). Distinguish between "user not found" (404 → acceptable to skip) and "service unavailable" (5xx/timeout → transient, should retry).

---

[HIGH] Consumer message ACK behavior on parse/use-case exceptions is undocumented and likely incorrect

Services affected: event-notifier
Location: `event-notifier/event_notifier/adapters/consumer.py:55-85`
Description: In `_handle`, if `from_http()` raises a parse exception, it is re-raised (line 59). If the use case raises, it propagates. FastStream's default behavior on an unhandled exception from a subscriber handler is to nack (or ack, depending on version and configuration) the message. There is no explicit `ack_policy` set on the subscriber, no prefetch configured, and no DLQ binding declared. If an unparseable message arrives (bad CloudEvent headers), it will be re-raised into FastStream and potentially requeued infinitely (creating a poison-pill loop), or silently acked and dropped, depending on FastStream's default. Neither outcome is acceptable in production.
Recommendation: Explicitly set `ack_policy` on the FastStream subscriber. For genuinely unparseable messages (bad format), catch the parse error, log it, and positively ACK (dead-letter manually or discard). For transient infrastructure errors, nack to allow requeue. Configure a DLQ on the queue to capture persistent failures.

---

[HIGH] No HTTP timeouts on any external API call

Services affected: event-notifier
Location: `event-notifier/event_notifier/ioc.py:54-66`, `event-notifier/event_notifier/infrastructure/users_client.py`, `event-notifier/event_notifier/infrastructure/channels/email.py`, `event-notifier/event_notifier/infrastructure/channels/telegram.py`
Description: All `httpx.AsyncClient` instances are created with no `timeout` argument, defaulting to httpx's global default of 5 seconds. While 5s is not catastrophic, it is not explicitly configured and can be silently changed by httpx version upgrades. More critically, there is no `connect_timeout` vs `read_timeout` distinction. A slow UniSender Go or Telegram API response will stall an OutboxSender worker for up to 5 seconds per record, degrading throughput on the single-threaded asyncio event loop. With a 1-second poll interval and batch size of 10, a full batch of slow records could take up to 50 seconds to process.
Recommendation: Explicitly pass `httpx.Timeout(connect=3.0, read=10.0, write=5.0)` (or similar values) to each `AsyncClient` constructor. Document the rationale for each value.

---

## MEDIUM

---

[MEDIUM] `get_contacts_by_email` uses email as `user_id` (legacy fallback) — idempotency key collision risk

Services affected: event-notifier
Location: `event-notifier/event_notifier/infrastructure/users_client.py:28-32`, `event-notifier/event_notifier/application/use_cases/process_domain_event.py:77`
Description: `get_contacts_by_email` uses `email` as the `user_id` field in `ChannelContact` (marked "legacy"). The outbox idempotency key is `"{event_id}:{user_id}:{channel}"`. If two routing rules for the same event produce the same email (possible if a user is both `volunteer` and `client` for different bookings, or data inconsistency), the key collapses and only one outbox record is written. Additionally, `get_contacts_by_email` is never called in the current use case flow — `ProcessDomainEventUseCase` calls only `get_contacts_by_id`. However, the method remains in the interface and users_client and is tested, creating a confusing dead code situation.
Recommendation: Either remove `get_contacts_by_email` from the interface and UsersClient (it is not called), or document clearly why it exists. Ensure the idempotency key includes `role` if both methods are used: `"{event_id}:{user_id}:{role}:{channel}"`.

---

[MEDIUM] `DOMAIN_EVENT_TO_TRIGGER` map is duplicated/divergent — template coverage gap

Services affected: event-notifier
Location: `event-notifier/event_notifier/event_types.py:9-15`, `event-notifier/event_notifier/infrastructure/channels/email.py:13-20`, `event-notifier/event_notifier/infrastructure/channels/telegram.py:12-19`
Description: `DOMAIN_EVENT_TO_TRIGGER` maps 5 event types (no `booking.rejected`). Email and Telegram `_TEMPLATE_MAP`/`_MESSAGE_TEMPLATES` both include `"BOOKING_REJECTED"`. The DB seed in `schema.py` does NOT seed a routing rule for `booking.rejected`. The architecture document (NOTIFICATION_SERVICE_ARCHITECTURE.md) references `meeting.created`, `meeting.cancelled` etc. (a different domain model entirely — "meeting" vs "booking"). The routing_rules DB seed uses `booking.*` keys. These inconsistencies suggest the NOTIFICATION_SERVICE_ARCHITECTURE.md is stale documentation from a prior design iteration and the templates contain a trigger (`BOOKING_REJECTED`) that can never be reached via the current routing pipeline.
Recommendation: Audit and align: (1) remove `BOOKING_REJECTED` from channel templates if there is no corresponding `booking.rejected` event type and routing rule, or add them end-to-end; (2) archive/update NOTIFICATION_SERVICE_ARCHITECTURE.md to reflect current implementation; (3) add a unit test that verifies every entry in `DOMAIN_EVENT_TO_TRIGGER` has a matching template in every active channel.

---

[MEDIUM] `fetch_pending_outbox` has no `status = 'processing'` state — retry window is invisible

Services affected: event-notifier
Location: `event-notifier/event_notifier/db/repository.py:75-89`, `event-notifier/event_notifier/db/schema.py:32`
Description: The outbox `status` field is constrained to `('pending', 'delivered', 'failed')`. There is no `'processing'` state. When an OutboxSender process crashes mid-delivery (after fetching but before `mark_delivered`), the record remains `'pending'` and will be retried on the next poll — which is correct for resilience. However, there is no way to distinguish "pending, never attempted" from "pending, currently being processed" from "pending, attempted N times". This makes operational visibility difficult: you cannot tell from the DB whether a `pending` record is stuck or just queued. The `retry_count` field partially addresses this but is only incremented on explicit failure, not on crash.
Recommendation: Add `'processing'` to the status enum and transition to it atomically in the SELECT-for-UPDATE (when that bug is fixed). Alternatively, add a `last_attempted_at TIMESTAMPTZ` column to provide operational observability without adding a state.

---

[MEDIUM] Outbox polling is a tight loop at 1-second interval — no backoff on empty batches

Services affected: event-notifier
Location: `event-notifier/event_notifier/adapters/outbox_sender.py:41-49`
Description: `OutboxSender.start()` polls every `poll_interval=1.0` seconds regardless of whether the previous poll returned any records. During quiet periods (no notifications), this generates one DB query per second continuously, adding unnecessary load to PostgreSQL. With the current schema's partial index (`WHERE status = 'pending'`), the queries are efficient but not free, especially if the index is regularly vacuumed.
Recommendation: Implement exponential backoff on empty polls (e.g., double the sleep up to a cap of 30 seconds, reset to 1 second when records are found). Alternatively use PostgreSQL LISTEN/NOTIFY to wake the sender when new outbox records are written.

---

[MEDIUM] Telegram channel silently falls back to generic message for unknown trigger events

Services affected: event-notifier
Location: `event-notifier/event_notifier/infrastructure/channels/telegram.py:34`
Description: `_MESSAGE_TEMPLATES.get(trigger_event, f"Уведомление: {trigger_event}")` uses a fallback that leaks the internal `trigger_event` string into the user-facing Telegram message body. If an unexpected event type reaches delivery, users receive a raw technical string like "Уведомление: BOOKING_REJECTED" instead of a proper error being logged. This behavior differs from `EmailChannel`, which returns `success=False` with an error on unknown trigger events.
Recommendation: Align Telegram with Email: return `DeliveryResult(success=False, ...)` for unknown trigger events rather than sending a fallback message. This prevents technical strings from reaching users and makes failures observable via the retry/failed pipeline.

---

[MEDIUM] `processed_events` check and outbox write are not atomic with RabbitMQ ACK

Services affected: event-notifier
Location: `event-notifier/event_notifier/adapters/consumer.py:55-85`, `event-notifier/event_notifier/application/use_cases/process_domain_event.py:25-102`
Description: The flow is: (1) check `is_processed` → (2) fetch routing rules → (3) HTTP call to event-users per recipient → (4) `write_outbox_atomically`. Between steps (1) and (4) there is no lock on the `processed_events` row. If two consumer instances receive the same message concurrently (possible with RabbitMQ's at-least-once delivery and prefetch > 1), both may pass the `is_processed` check before either writes to `processed_events`. The `ON CONFLICT DO NOTHING` in step (4) ensures the DB stays consistent, but both instances will have made N HTTP calls to event-users. The outbox records themselves are protected by the idempotency key unique constraint, so actual duplicate sends are prevented at the DB level. The real risk is unnecessary fan-out HTTP calls during concurrent duplicate delivery.
Recommendation: This is an acceptable trade-off for the stated load (20–30 notifications/minute). Document the behavior explicitly. For higher loads, consider using `INSERT INTO processed_events ... ON CONFLICT DO NOTHING RETURNING cloud_event_id` as the first statement in the transaction to get early-exit without a separate SELECT.

---

[MEDIUM] No consumer test — the adapter layer has zero test coverage

Services affected: event-notifier
Location: `event-notifier/tests/` (no `test_consumer.py`)
Description: `NotificationConsumer` (`adapters/consumer.py`) is untested. It contains logic for CloudEvent parsing, event type filtering, `booking_id` extraction with two different fallback paths (`ce.get("booking_id") or (ce.data or {}).get("booking_id", "")`), and dispatching to the use case. The complex `ce.get()` fallback chain on line 67 is untested. There are also no integration tests for the FastStream broker wiring.
Recommendation: Add unit tests for `NotificationConsumer._handle` covering: valid CloudEvent, unknown event type (skipped), malformed CloudEvent (exception raised), and `booking_id` extraction fallback paths.

---

## LOW

---

[LOW] `declare=False` on consumer queue — creation responsibility undocumented

Services affected: event-notifier
Location: `event-notifier/event_notifier/adapters/consumer.py:37`
Description: The RabbitQueue is declared with `declare=False`, meaning the queue must already exist when the service starts. If the queue does not exist, FastStream will fail to bind the subscriber and the consumer will silently not receive any messages (or throw at startup depending on FastStream version). There is no documentation in the service's CLAUDE.md or deployment scripts about which service creates this queue. event-receiver does declare queues via `ITopologyManager.ensure_topology()` on startup, but this requires event-receiver to start before event-notifier, and the queue name must match exactly.
Recommendation: Document the startup dependency (event-receiver must start first, or the queue must be pre-created by an infrastructure-as-code tool). Consider adding a startup health check that verifies queue existence before starting the consumer. Alternatively, set `declare=True` with idempotent queue creation.

---

[LOW] `API key logged` risk — `unisender_api_key` could appear in structured log output

Services affected: event-notifier
Location: `event-notifier/event_notifier/infrastructure/channels/email.py:54-64`
Description: The `payload` dict passed to UniSender includes `"api_key": self._api_key`. If any log call accidentally serializes the full `payload` (e.g., during debugging or if a future developer adds `logger.debug("payload", payload=payload)`), the API key would appear in logs. Currently no such call exists, but the risk is latent.
Recommendation: Strip the `api_key` from the dict before any potential logging, or restructure the payload so credentials are passed as HTTP headers rather than in the JSON body (where UniSender Go supports it).

---

[LOW] No `retry` status in outbox — stuck records after process crash are invisible

Services affected: event-notifier
Location: `event-notifier/event_notifier/db/schema.py:32`, `event-notifier/event_notifier/db/repository.py:114-127`
Description: `mark_retry` updates `status` back to `'pending'` with a future `scheduled_at`. There is no `'retrying'` status. This is consistent with the current design but means a `failed` record and a `pending, retry_count=4` record look similar in status. Operational dashboards cannot easily distinguish "will retry soon" from "never tried."
Recommendation: Minor: add `retry_count > 0` to monitoring queries to surface records in active retry. This does not require a schema change.

---

[LOW] `NOTIFICATION_SERVICE_ARCHITECTURE.md` is stale and misleading

Services affected: event-notifier
Location: `event-notifier/NOTIFICATION_SERVICE_ARCHITECTURE.md`
Description: This document describes a completely different design: it uses `meeting.*` event types instead of `booking.*`, references Jinja2 template rendering, a `notification_templates` table, `delivery_log` table, `device_tokens` table, WhatsApp channel, aiohttp (not httpx), and pydantic-settings with `NOTIFY_` prefix — none of which exist in the current codebase. It appears to be a design specification from an earlier iteration that was not updated when the implementation diverged.
Recommendation: Either archive this document or replace it with current architecture documentation. Having stale docs of this magnitude is a significant onboarding hazard and makes it impossible for new engineers to understand the actual system.

---

[LOW] `pyproject.toml` references external git dependency for `event-schemas`

Services affected: event-notifier
Location: `event-notifier/pyproject.toml:11`
Description: `event-schemas` is declared as `event-schemas @ git+https://github.com/Lelikov/event-schemas.git` with no pinned commit SHA or tag. This is a mutable dependency — `uv sync` on different dates can produce different resolved versions. In practice, `event-schemas` does not appear to be imported anywhere in the current event-notifier Python source (no `from event_schemas import ...` found), making it an unused dependency that still carries version instability risk.
Recommendation: Either remove the dependency if unused, or pin to a specific commit SHA (`@ git+https://github.com/Lelikov/event-schemas.git@<sha>`). Prefer publishing `event-schemas` to a private PyPI index or using uv workspaces for monorepo management.

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 3     |
| HIGH     | 5     |
| MEDIUM   | 6     |
| LOW      | 4     |
| **Total**| **18**|

### Key findings recap

**Production blockers (service cannot function correctly):**
1. FCM vars crash startup even though FCM/Push is disabled.
2. `FOR UPDATE SKIP LOCKED` without a transaction makes the locking entirely ineffective, enabling duplicate deliveries on multi-instance or high-frequency polling.
3. Queue name mismatch: config default `events.notifications` vs. documented intent `events.notification.commands` — the wrong queue will be consumed in default deployments.

**Architecture gaps:**
- The delivery result publisher (`notification.*.message_sent` events back to event-receiver) is completely absent despite routing rules existing on the receiver side.
- `NOTIFICATION_SERVICE_ARCHITECTURE.md` describes a different service; it should not exist in this state.

**Resilience gaps:**
- No explicit ACK/nack policy on the consumer — poison-pill infinite requeue risk.
- event-users outage causes silent message loss (ACK without notification written).
- No HTTP timeouts explicitly configured.

---

## Production Readiness Assessment

**Verdict: NO-GO**

### Hard Blockers (must fix before any production deployment)

1. **[CRITICAL] FCM env vars crash startup** — service cannot start in any environment that does not supply `FCM_PROJECT_ID` and `FCM_SERVICE_ACCOUNT_JSON`, which is every environment where Push is not yet configured.
2. **[CRITICAL] `FOR UPDATE SKIP LOCKED` without transaction** — the primary concurrency-safety mechanism of the outbox pattern is broken; duplicate notification delivery is likely under any multi-instance or rapid-poll scenario.
3. **[CRITICAL] Queue name mismatch** — with defaults, the service consumes `events.notifications` but the architecture intends `events.notification.commands`. The service will either process the wrong messages or miss the right ones depending on which queue is actually populated.
4. **[HIGH] No delivery result events published** — the `notification.*.message_sent` pipeline to event-receiver is entirely unimplemented; downstream consumers expecting delivery confirmations receive nothing.
5. **[HIGH] Silent message loss when event-users is down** — the consumer ACKs messages without writing outbox records when contact resolution fails transiently, permanently losing notification requests.
6. **[HIGH] No explicit consumer ACK policy / no DLQ** — malformed or persistently failing messages have no safe disposal path; behavior on exception is undefined by FastStream defaults.
