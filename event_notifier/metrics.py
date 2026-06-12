"""Prometheus metrics for event-notifier.

Module-level metric objects (idiomatic for prometheus-client). Consumer RED
metrics are recorded by the RabbitMQ consumer; delivery counters and outbox
gauges by the outbox sender. Exposed via GET /metrics on the health HTTP app.
"""

from time import perf_counter

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

MESSAGES_PROCESSED_TOTAL = Counter(
    "messages_processed_total",
    "Consumed RabbitMQ messages by queue, event type and outcome (ok, retried, rejected).",
    ["queue", "event_type", "outcome"],
)
MESSAGE_PROCESSING_SECONDS = Histogram(
    "message_processing_seconds",
    "Message processing duration in seconds by queue.",
    ["queue"],
)

DELIVERIES_TOTAL = Counter(
    "notifier_deliveries_total",
    "Outbox delivery attempts by channel, trigger event and outcome (delivered, retried, failed).",
    ["channel", "trigger", "outcome"],
)
OUTBOX_DEPTH = Gauge(
    "notifier_outbox_depth",
    "notification_outbox rows by status; refreshed by the outbox poll loop.",
    ["status"],
)
OUTBOX_OLDEST_PENDING_AGE = Gauge(
    "notifier_outbox_oldest_pending_age_seconds",
    "Age in seconds of the oldest pending outbox row (0 when none); refreshed by the outbox poll loop.",
)


def record_message(*, queue: str, event_type: str, outcome: str, started_at: float) -> None:
    MESSAGES_PROCESSED_TOTAL.labels(queue=queue, event_type=event_type, outcome=outcome).inc()
    MESSAGE_PROCESSING_SECONDS.labels(queue=queue).observe(perf_counter() - started_at)


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
