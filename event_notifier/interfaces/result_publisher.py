from typing import Protocol

from event_notifier.domain.models.notification import OutboxRecord


class IDeliveryResultPublisher(Protocol):
    async def publish_delivered(self, record: OutboxRecord, message_id: str | None) -> None:
        """Publish a notification.*.message_sent result event. Must never raise."""
        ...
