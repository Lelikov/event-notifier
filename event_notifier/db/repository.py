"""SqlExecutor-based implementation of INotificationRepository."""

import json

import structlog

from event_notifier.domain.models.notification import OutboxRecord, RoutingRule
from event_notifier.interfaces.sql import ISqlExecutor

logger = structlog.get_logger(__name__)


class NotificationRepository:
    def __init__(self, sql: ISqlExecutor) -> None:
        self._sql = sql

    async def get_routing_rules(self, event_type: str) -> list[RoutingRule]:
        rows = await self._sql.fetch_all(
            "SELECT event_type, recipient_field, recipient_role "
            "FROM routing_rules WHERE event_type = :event_type AND active = TRUE",
            {"event_type": event_type},
        )
        return [
            RoutingRule(
                event_type=row["event_type"],
                recipient_field=row["recipient_field"],
                recipient_role=row["recipient_role"],
            )
            for row in rows
        ]

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
    ) -> None:
        """Insert outbox records and mark event as processed.

        Transaction management is handled by the AsyncSession in the DI scope.
        """
        await self._sql.execute(
            "INSERT INTO processed_events (cloud_event_id) VALUES (:cloud_event_id) ON CONFLICT DO NOTHING",
            {"cloud_event_id": cloud_event_id},
        )
        for rec in records:
            await self._sql.execute(
                """
                INSERT INTO notification_outbox
                    (idempotency_key, cloud_event_id, booking_id, user_id,
                     recipient_address, recipient_role, channel, event_type, template_context)
                VALUES (:idempotency_key, :cloud_event_id, :booking_id, :user_id,
                        :recipient_address, :recipient_role, :channel, :event_type,
                        :template_context::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                {
                    "idempotency_key": rec["idempotency_key"],
                    "cloud_event_id": rec["cloud_event_id"],
                    "booking_id": rec["booking_id"],
                    "user_id": rec["user_id"],
                    "recipient_address": rec["recipient_address"],
                    "recipient_role": rec["recipient_role"],
                    "channel": rec["channel"],
                    "event_type": rec["event_type"],
                    "template_context": json.dumps(rec["template_context"]),
                },
            )
        await self._sql.commit()
        logger.debug("Outbox written atomically", cloud_event_id=cloud_event_id, count=len(records))

    async def fetch_pending_outbox(self, batch_size: int = 10) -> list[OutboxRecord]:
        rows = await self._sql.fetch_all(
            """
            UPDATE notification_outbox
            SET status = 'processing', updated_at = NOW()
            WHERE id IN (
                SELECT id FROM notification_outbox
                WHERE status = 'pending' AND scheduled_at <= NOW()
                ORDER BY scheduled_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id::text, cloud_event_id, booking_id, user_id,
                      recipient_address, recipient_role, channel, event_type,
                      template_context, retry_count, max_retries
            """,
            {"batch_size": batch_size},
        )
        await self._sql.commit()
        return [
            OutboxRecord(
                id=row["id"],
                cloud_event_id=row["cloud_event_id"],
                booking_id=row["booking_id"],
                user_id=row["user_id"],
                recipient_address=row["recipient_address"],
                recipient_role=row["recipient_role"],
                channel=row["channel"],
                event_type=row["event_type"],
                template_context=dict(row["template_context"]) if row["template_context"] else {},
                retry_count=row["retry_count"],
                max_retries=row["max_retries"],
            )
            for row in rows
        ]

    async def mark_delivered(self, record_id: str) -> None:
        await self._sql.execute(
            "UPDATE notification_outbox SET status='delivered', updated_at=NOW() WHERE id=:id::uuid",
            {"id": record_id},
        )
        await self._sql.commit()

    async def mark_retry(self, record_id: str, retry_count: int, delay_seconds: int) -> None:
        await self._sql.execute(
            """
            UPDATE notification_outbox
            SET retry_count = :retry_count,
                scheduled_at = NOW() + (:delay || ' seconds')::interval,
                status = 'pending',
                updated_at = NOW()
            WHERE id = :id::uuid
            """,
            {"id": record_id, "retry_count": retry_count, "delay": str(delay_seconds)},
        )
        await self._sql.commit()

    async def mark_failed(self, record_id: str) -> None:
        await self._sql.execute(
            "UPDATE notification_outbox SET status='failed', updated_at=NOW() WHERE id=:id::uuid",
            {"id": record_id},
        )
        await self._sql.commit()

    async def cleanup_processed_events(self, days: int = 7) -> None:
        """Delete processed_events older than the specified number of days."""
        await self._sql.execute(
            "DELETE FROM processed_events WHERE processed_at < NOW() - (:days || ' days')::interval",
            {"days": str(days)},
        )
        await self._sql.commit()
        logger.info("Cleaned up processed_events", older_than_days=days)
