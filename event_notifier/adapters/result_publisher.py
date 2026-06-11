"""Publishes notification.*.message_sent delivery-result CloudEvents to event-receiver.

Fire-and-forget: the notification is already delivered when this runs, so any
publish failure is logged and swallowed — it must never fail or retry the
outbox record. Event ids are deterministic (UUIDv5 of the idempotency key) so
re-published results deduplicate downstream.
"""

import json
import uuid
from datetime import UTC, datetime

import httpx
import structlog
from cloudevents.core.bindings.http import to_binary
from cloudevents.core.formats.json import JSONFormat
from cloudevents.core.v1.event import CloudEvent
from event_schemas.types import EventType

from event_notifier.domain.models.notification import OutboxRecord

logger = structlog.get_logger(__name__)

NOTIFIER_SOURCE = "event-notifier"

# Fixed namespace for deterministic result-event ids.
_RESULT_EVENT_ID_NAMESPACE = uuid.UUID("3f3c7e9a-1d24-4b6e-8a0f-5c9b2e7d4a11")

_CHANNEL_TO_EVENT_TYPE: dict[str, EventType] = {
    "email": EventType.NOTIFICATION_EMAIL_SENT,
    "telegram": EventType.NOTIFICATION_TELEGRAM_SENT,
    "push": EventType.NOTIFICATION_PUSH_SENT,
}


def _result_payload(record: OutboxRecord, message_id: str | None) -> dict:
    base = {
        "email": record.recipient_email,
        "recipient_role": record.recipient_role,
        "trigger_event": record.trigger_event,
        "booking_uid": record.booking_id,
    }
    if record.channel == "email":
        return {**base, "job_id": message_id}
    if record.channel == "push":
        return {**base, "device_token": record.recipient_address, "message_id": message_id}
    return base


class DeliveryResultPublisher:
    """POSTs binary-mode CloudEvents to event-receiver's generic ingest endpoint."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None,
        endpoint_url: str = "",
        api_key: str = "",
        source: str = NOTIFIER_SOURCE,
    ) -> None:
        self._client = http_client if endpoint_url else None
        self._endpoint_url = endpoint_url
        self._api_key = api_key
        self._source = source
        if self._client is None:
            logger.warning("EVENTS_ENDPOINT_URL not configured: delivery-result events are disabled")

    async def publish_delivered(self, record: OutboxRecord, message_id: str | None) -> None:
        if self._client is None:
            return

        event_type = _CHANNEL_TO_EVENT_TYPE.get(record.channel)
        if event_type is None:
            logger.warning("No result event type for channel, skipping", channel=record.channel)
            return

        ce = CloudEvent(
            {
                "type": event_type.value,
                "source": self._source,
                "id": str(uuid.uuid5(_RESULT_EVENT_ID_NAMESPACE, f"result:{record.id}")),
                "time": datetime.now(UTC),
                "specversion": "1.0",
            },
            json.dumps(_result_payload(record, message_id)).encode(),
        )
        message = to_binary(ce, JSONFormat())
        headers = dict(message.headers)
        headers["content-type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = self._api_key

        try:
            response = await self._client.post(self._endpoint_url, headers=headers, content=message.body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Failed to publish delivery-result event (delivery itself succeeded)",
                event_type=event_type.value,
                outbox_id=record.id,
                error=str(exc),
            )
            return
        logger.info("Delivery-result event published", event_type=event_type.value, outbox_id=record.id)
