"""Protocol interface for notification repository."""

from typing import Any, Protocol

from event_notifier.domain.models.notification import OutboxRecord, OutboxStats


class INotificationRepository(Protocol):
    async def is_processed(self, cloud_event_id: str) -> bool: ...

    async def write_outbox_atomically(
        self,
        cloud_event_id: str,
        records: list[dict[str, Any]],
    ) -> bool:
        """Claim the event (processed_events) + write outbox records in one transaction.

        Returns False when the event was already claimed by a concurrent worker.
        """
        ...

    async def fetch_pending_outbox(self, batch_size: int = 10) -> list[OutboxRecord]: ...

    async def reap_stale_processing(self, stale_after_seconds: int = 300) -> int: ...

    async def mark_delivered(self, record_id: str) -> None: ...

    async def mark_retry(
        self, record_id: str, retry_count: int, delay_seconds: int, error: str | None = None
    ) -> None: ...

    async def mark_failed(self, record_id: str, error: str | None = None) -> None: ...

    async def outbox_stats(self) -> OutboxStats:
        """Row counts by status + oldest pending age (monitoring gauges)."""
        ...

    async def cleanup_processed_events(self, days: int = 7) -> None: ...

    async def healthcheck(self) -> bool: ...
