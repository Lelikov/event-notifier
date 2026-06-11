# event-notifier Audit — v2 (2026-06-11)

Source findings: `../docs/audit/v2/findings/event-notifier.json` plus the
event-notifier entries of `rabbitmq-topology`, `delivery-reliability`,
`security` and `flow-e2e`. All fixes live on branch `audit-fixes`
(commits `7a45e38..` — contracts wave, then the command-path redesign wave).

## Status Ledger

### CRITICAL — all fixed

| Finding | Fix |
|---------|-----|
| `httpx.Timeout` without `pool=` crashes DI resolution at boot | All four kwargs set explicitly; regression test resolves the full startup graph through the real Dishka container (`tests/test_ioc.py`) |
| FastStream handler signature failed pydantic validation — handler never ran | Handler takes the raw message via `Context("message")`; verified end-to-end with `TestRabbitBroker` |
| Payload read at wrong level (`{original, normalized}` envelope ignored) | Consumer parses `EventEnvelope` and validates `original` as `NotificationCommandPayload` (commit `7a45e38` + consumer redesign) |
| Recipient contract mismatch (`{email, role}` vs required `user_id`) | Recipients come from the payload; `user_id`/`time_zone` merged from `normalized.participants` by email |
| OutboxSender marked every record failed via dead `DOMAIN_EVENT_TO_TRIGGER` lookup | `trigger_event` taken from the payload, stored on the outbox row, parsed as `TriggerEvent` at send time |
| Single APP-scoped `AsyncSession` shared across tasks | `SqlExecutor` opens a fresh session per operation; multi-statement units use `transaction()` |
| Queue declaration argument mismatch → PRECONDITION_FAILED | Queue declared from the canonical `event_schemas.queues.NOTIFICATION_COMMANDS_QUEUE` spec |
| UsersClient called `GET /users/{id}` (guaranteed 404, recipient dropped) | Calls `GET /api/users/id/{user_id}`; 404 → email-only degrade, transport/5xx → `UsersServiceError` → NACK/retry |

### HIGH — all fixed

| Finding | Fix |
|---------|-----|
| Rows stuck in `status='processing'` lost forever after crash | Reaper returns stale rows (>300 s) to `pending` every 60 s, counting the reap as an attempt |
| Reject-without-requeue to an undeclared DLX — transient outages lost notifications | DLX + own DLQ declared idempotently at startup; explicit ack policy: transient → backoff + NACK(requeue), poison → DLQ |
| No delivery-result events published | `DeliveryResultPublisher` POSTs `notification.*.message_sent` binary CloudEvents to event-receiver (deterministic UUIDv5 ids, fire-and-forget) |
| UniSender template_id slugs + raw envelope in substitutions | Template UUIDs from `UNISENDER_TEMPLATE_IDS` config; substitutions flattened to scalars only |
| No 4xx/5xx classification, ~9-minute retry budget, terminal `failed` without redrive | `DeliveryResult.retryable` (408/429/5xx/transport vs permanent 4xx); capped exponential backoff to 30 min, `max_retries=10`; documented SQL redrive |
| Stale `.env.example` (phantom queue, sync DATABASE_URL) | Rewritten: asyncpg DSN, canonical-queue note, all new vars |

### MEDIUM — all fixed except locale (cross-service)

| Finding | Fix |
|---------|-----|
| Routing-rules machinery unreachable dead code | Deleted (`routing_rules` table dropped in migration 002; `process_domain_event`, `routing.py`, `event_types.py` removed) |
| `booking_id` always empty (`ce-bookingid` vs `booking_id`, wrapper fallback) | Reads canonical `BOOKING_ID_ATTRIBUTE`, falls back to `original.booking_id` |
| Test suite validated a non-existent wire contract; consumer/IoC zero coverage | 80 tests against the real envelope, `TestRabbitBroker` wire test, IoC container resolution tests, repository SQL tests |
| Telegram sent raw internal trigger string for unknown triggers | Unknown triggers fail permanently; bodies come from Jinja2 templates only |
| `is_processed` not atomic with outbox write (duplicate fan-out) | `write_outbox_atomically`: processed_events claim + inserts in ONE transaction; concurrent duplicate returns False |
| No per-recipient localization (times raw UTC, time_zone unused, locale dropped) | **Partially fixed (notifier side):** per-recipient `start_time_local`/`end_time_local`/`time_zone` from `normalized.participants[].time_zone`. **Open (cross-service):** locale/language never reaches the envelope — needs event-receiver/event-schemas change |

### LOW — all fixed

| Finding | Fix |
|---------|-----|
| Fixed 1 s outbox polling, no backoff | Exponential idle backoff to 30 s, reset on activity |
| `get_contacts_by_email` dead code with swallow-all fallback | UsersClient rewritten; single `get_user_contacts` with explicit 404/transport semantics |
| UniSender api_key in JSON body (credential-logging risk) | API key sent only via `X-API-KEY` header |
| Documentation drift | All docs rewritten 2026-06-11; stale `NOTIFICATION_SERVICE_ARCHITECTURE.md` deleted |
| `/health` always returned ok | Deep checks: consumer started, sender task alive, DB reachable; 503 when degraded |

## Open Items

1. **Locale/language localization** — cross-service: cal.com `language.locale` is
   dropped at ingress; `NormalizedParticipant`/envelope carry no locale. Requires
   event-receiver + event-schemas changes before the notifier can select template
   language per recipient.
2. **PushChannel** — implemented and classification-aligned but not registered
   (FCM credentials and an access-token provider pending).
3. **Metrics/alerting** — `failed` outbox rows and DLQ depth are only visible via
   logs and SQL; no Prometheus counters.
