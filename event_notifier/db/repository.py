"""SqlExecutor-based implementation of INotificationRepository."""

import json

import structlog

from event_notifier.domain.models.notification import OutboxRecord
from event_notifier.interfaces.sql import ISqlExecutor

logger = structlog.get_logger(__name__)

_OUTBOX_COLUMNS = """
    id::text, cloud_event_id, booking_id, user_id, recipient_email,
    recipient_address, recipient_role, channel, trigger_event,
    template_context, retry_count, max_retries
"""


def _row_to_record(row: dict) -> OutboxRecord:
    return OutboxRecord(
        id=row["id"],
        cloud_event_id=row["cloud_event_id"],
        booking_id=row["booking_id"],
        user_id=row["user_id"],
        recipient_email=row["recipient_email"],
        recipient_address=row["recipient_address"],
        recipient_role=row["recipient_role"],
        channel=row["channel"],
        trigger_event=row["trigger_event"],
        template_context=dict(row["template_context"]) if row["template_context"] else {},
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
    )


class NotificationRepository:
    def __init__(self, sql: ISqlExecutor) -> None:
        self._sql = sql

    async def is_processed(self, cloud_event_id: str) -> bool:
        row = await self._sql.fetch_one(
            "SELECT 1 FROM processed_events WHERE cloud_event_id = :cloud_event_id",
            {"cloud_event_id": cloud_event_id},
        )
        return row is not None

    async def write_outbox_atomically(
        self,
        cloud_event_id: str,
        records: list[dict],
    ) -> bool:
        """Mark the event processed and insert its outbox records in ONE transaction.

        Returns False (writing nothing) when another worker already claimed the
        event — the processed_events insert is the first statement, so the claim
        itself is atomic.
        """
        async with self._sql.transaction() as tx:
            claimed = await tx.fetch_one(
                "INSERT INTO processed_events (cloud_event_id) VALUES (:cloud_event_id) "
                "ON CONFLICT DO NOTHING RETURNING cloud_event_id",
                {"cloud_event_id": cloud_event_id},
            )
            if claimed is None:
                logger.info("Event already claimed by another worker, skipping", cloud_event_id=cloud_event_id)
                return False
            for rec in records:
                await tx.execute(
                    """
                    INSERT INTO notification_outbox
                        (idempotency_key, cloud_event_id, booking_id, user_id, recipient_email,
                         recipient_address, recipient_role, channel, trigger_event, template_context)
                    VALUES (:idempotency_key, :cloud_event_id, :booking_id, :user_id, :recipient_email,
                            :recipient_address, :recipient_role, :channel, :trigger_event,
                            CAST(:template_context AS JSONB))
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    {
                        "idempotency_key": rec["idempotency_key"],
                        "cloud_event_id": rec["cloud_event_id"],
                        "booking_id": rec["booking_id"],
                        "user_id": rec["user_id"],
                        "recipient_email": rec["recipient_email"],
                        "recipient_address": rec["recipient_address"],
                        "recipient_role": rec["recipient_role"],
                        "channel": rec["channel"],
                        "trigger_event": rec["trigger_event"],
                        "template_context": json.dumps(rec["template_context"]),
                    },
                )
        logger.debug("Outbox written atomically", cloud_event_id=cloud_event_id, count=len(records))
        return True

    async def fetch_pending_outbox(self, batch_size: int = 10) -> list[OutboxRecord]:
        rows = await self._sql.fetch_all(
            f"""
            UPDATE notification_outbox
            SET status = 'processing', updated_at = NOW()
            WHERE id IN (
                SELECT id FROM notification_outbox
                WHERE status = 'pending' AND scheduled_at <= NOW()
                ORDER BY scheduled_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            RETURNING {_OUTBOX_COLUMNS}
            """,
            {"batch_size": batch_size},
        )
        return [_row_to_record(dict(row)) for row in rows]

    async def reap_stale_processing(self, stale_after_seconds: int = 300) -> int:
        """Return crashed-mid-delivery rows (stuck in 'processing') to the pending pool.

        Counts the reap as an attempt so a row crashing the sender repeatedly
        still converges to 'failed'.
        """
        rows = await self._sql.fetch_all(
            """
            UPDATE notification_outbox
            SET status = 'pending',
                retry_count = retry_count + 1,
                updated_at = NOW()
            WHERE status = 'processing'
              AND updated_at < NOW() - (:stale || ' seconds')::interval
            RETURNING id::text
            """,
            {"stale": str(stale_after_seconds)},
        )
        if rows:
            logger.warning("Reaped stale processing outbox rows", count=len(rows))
        return len(rows)

    async def mark_delivered(self, record_id: str) -> None:
        await self._sql.execute(
            "UPDATE notification_outbox SET status='delivered', updated_at=NOW() WHERE id=CAST(:id AS UUID)",
            {"id": record_id},
        )

    async def mark_retry(self, record_id: str, retry_count: int, delay_seconds: int, error: str | None = None) -> None:
        await self._sql.execute(
            """
            UPDATE notification_outbox
            SET retry_count = :retry_count,
                scheduled_at = NOW() + (:delay || ' seconds')::interval,
                status = 'pending',
                last_error = :error,
                updated_at = NOW()
            WHERE id = CAST(:id AS UUID)
            """,
            {"id": record_id, "retry_count": retry_count, "delay": str(delay_seconds), "error": error},
        )

    async def mark_failed(self, record_id: str, error: str | None = None) -> None:
        """Terminal failure. Operators redrive with:
        UPDATE notification_outbox SET status='pending', retry_count=0 WHERE status='failed' AND ...;
        """
        await self._sql.execute(
            "UPDATE notification_outbox SET status='failed', last_error=:error, updated_at=NOW() "
            "WHERE id=CAST(:id AS UUID)",
            {"id": record_id, "error": error},
        )

    async def cleanup_processed_events(self, days: int = 7) -> None:
        """Delete processed_events older than the specified number of days."""
        await self._sql.execute(
            "DELETE FROM processed_events WHERE processed_at < NOW() - (:days || ' days')::interval",
            {"days": str(days)},
        )
        logger.info("Cleaned up processed_events", older_than_days=days)

    async def healthcheck(self) -> bool:
        row = await self._sql.fetch_one("SELECT 1 AS ok", {})
        return row is not None
