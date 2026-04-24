"""Protocol interface for notification repository."""

from typing import Any, Protocol

from event_notifier.domain.models.notification import OutboxRecord, RoutingRule


class INotificationRepository(Protocol):
    async def get_routing_rules(self, event_type: str) -> list[RoutingRule]: ...

    async def is_processed(self, cloud_event_id: str) -> bool: ...

    async def write_outbox_atomically(
        self,
        cloud_event_id: str,
        records: list[dict[str, Any]],
    ) -> None:
        """Write outbox records + mark event as processed in one transaction."""
        ...

    async def fetch_pending_outbox(self, batch_size: int = 10) -> list[OutboxRecord]: ...

    async def mark_delivered(self, record_id: str) -> None: ...

    async def mark_retry(self, record_id: str, retry_count: int, delay_seconds: int) -> None: ...

    async def mark_failed(self, record_id: str) -> None: ...
