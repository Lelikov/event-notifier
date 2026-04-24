"""asyncpg-based implementation of INotificationRepository."""

import json
from typing import Any

import asyncpg
import structlog

from event_notifier.domain.models.notification import OutboxRecord, RoutingRule

logger = structlog.get_logger(__name__)


class NotificationRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_routing_rules(self, event_type: str) -> list[RoutingRule]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_type, recipient_field, recipient_role "
                "FROM routing_rules WHERE event_type = $1 AND active = TRUE",
                event_type,
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
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM processed_events WHERE cloud_event_id = $1",
                cloud_event_id,
            )
        return row is not None

    async def write_outbox_atomically(
        self,
        cloud_event_id: str,
        records: list[dict[str, Any]],
    ) -> None:
        """Insert outbox records and mark event as processed in a single transaction."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO processed_events (cloud_event_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    cloud_event_id,
                )
                for rec in records:
                    await conn.execute(
                        """
                        INSERT INTO notification_outbox
                            (idempotency_key, cloud_event_id, booking_id, user_id,
                             recipient_address, recipient_role, channel, event_type, template_context)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        rec["idempotency_key"],
                        rec["cloud_event_id"],
                        rec["booking_id"],
                        rec["user_id"],
                        rec["recipient_address"],
                        rec["recipient_role"],
                        rec["channel"],
                        rec["event_type"],
                        json.dumps(rec["template_context"]),
                    )
        logger.debug("Outbox written atomically", cloud_event_id=cloud_event_id, count=len(records))

    async def fetch_pending_outbox(self, batch_size: int = 10) -> list[OutboxRecord]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE notification_outbox
                    SET status = 'processing', updated_at = NOW()
                    WHERE id IN (
                        SELECT id FROM notification_outbox
                        WHERE status = 'pending' AND scheduled_at <= NOW()
                        ORDER BY scheduled_at
                        LIMIT $1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id::text, cloud_event_id, booking_id, user_id,
                              recipient_address, recipient_role, channel, event_type,
                              template_context, retry_count, max_retries
                    """,
                    batch_size,
                )
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
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE notification_outbox SET status='delivered', updated_at=NOW() WHERE id=$1::uuid",
                record_id,
            )

    async def mark_retry(self, record_id: str, retry_count: int, delay_seconds: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE notification_outbox
                SET retry_count=$2,
                    scheduled_at = NOW() + ($3 || ' seconds')::interval,
                    updated_at = NOW()
                WHERE id=$1::uuid
                """,
                record_id,
                retry_count,
                str(delay_seconds),
            )

    async def mark_failed(self, record_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE notification_outbox SET status='failed', updated_at=NOW() WHERE id=$1::uuid",
                record_id,
            )

    async def cleanup_processed_events(self, days: int = 7) -> int:
        """Delete processed_events older than the specified number of days."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM processed_events WHERE processed_at < NOW() - $1::interval",
                f"{days} days",
            )
        count = int(result.split()[-1]) if result else 0
        logger.info("Cleaned up processed_events", deleted=count, older_than_days=days)
        return count
