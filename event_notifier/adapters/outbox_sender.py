"""Background outbox sender: polls notification_outbox and delivers via channel adapters.

Retry policy:
- Permanent failures (4xx other than 408/429, missing template, unknown
  channel/trigger) are marked 'failed' immediately — retrying an identical
  bad request only burns the provider rate limit.
- Transient failures (timeouts, 408/429/5xx) back off exponentially
  (10s, 20s, 40s, ... capped at 30 min) up to max_retries (default 10),
  giving a total retry window of several hours for provider outages.
- Rows stuck in 'processing' (crash mid-delivery) are reaped back to
  'pending' after reap_stale_seconds.
- 'failed' is terminal but redriveable by operators:
  UPDATE notification_outbox SET status='pending', retry_count=0 WHERE status='failed' AND ...;
"""

import asyncio

import structlog
from event_schemas.types import TriggerEvent

from event_notifier import metrics
from event_notifier.domain.models.notification import ChannelContact, ChannelType, OutboxRecord, OutboxStatus
from event_notifier.interfaces.channels import INotificationChannel
from event_notifier.interfaces.repository import INotificationRepository
from event_notifier.interfaces.result_publisher import IDeliveryResultPublisher

logger = structlog.get_logger(__name__)

_MAX_RETRY_DELAY_SECONDS = 1800
_MAX_IDLE_POLL_INTERVAL = 30.0


def _retry_delay_seconds(retry_count: int) -> int:
    """Capped exponential backoff: 10s, 20s, 40s, ..., capped at 30 minutes."""
    return min(10 * 2 ** (retry_count - 1), _MAX_RETRY_DELAY_SECONDS)


class OutboxSender:
    def __init__(
        self,
        *,
        repository: INotificationRepository,
        channels: dict[ChannelType, INotificationChannel],
        result_publisher: IDeliveryResultPublisher,
        batch_size: int = 10,
        poll_interval: float = 1.0,
        reap_interval: float = 60.0,
        reap_stale_seconds: int = 300,
    ) -> None:
        self._repository = repository
        self._channels = channels
        self._result_publisher = result_publisher
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._reap_interval = reap_interval
        self._reap_stale_seconds = reap_stale_seconds
        self._idle_interval = poll_interval
        self._running = False

    async def run_once(self) -> int:
        """Process one batch of pending outbox records; returns the batch size."""
        records = await self._repository.fetch_pending_outbox(self._batch_size)
        for record in records:
            await self._process_record(record)
        return len(records)

    async def start(self) -> None:
        self._running = True
        seconds_since_reap = self._reap_interval  # reap immediately on startup (crash recovery)
        logger.info("OutboxSender started", poll_interval=self._poll_interval)
        while self._running:
            try:
                if seconds_since_reap >= self._reap_interval:
                    await self._repository.reap_stale_processing(self._reap_stale_seconds)
                    await self.refresh_outbox_gauges()
                    seconds_since_reap = 0.0
                processed = await self.run_once()
                self._idle_interval = self._next_idle_interval(processed)
            except Exception:
                logger.exception("OutboxSender loop error")
            await asyncio.sleep(self._idle_interval)
            seconds_since_reap += self._idle_interval

    async def refresh_outbox_gauges(self) -> None:
        """Refresh outbox depth / oldest-pending-age gauges (piggybacks on the reap cadence)."""
        stats = await self._repository.outbox_stats()
        for status in OutboxStatus:
            metrics.OUTBOX_DEPTH.labels(status=status.value).set(stats.counts_by_status.get(status.value, 0))
        metrics.OUTBOX_OLDEST_PENDING_AGE.set(stats.oldest_pending_age_seconds)

    def _next_idle_interval(self, processed: int) -> float:
        """Exponential backoff on empty polls (cap 30s), reset on activity."""
        if processed > 0:
            return self._poll_interval
        return min(self._idle_interval * 2, _MAX_IDLE_POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
        logger.info("OutboxSender stopped")

    async def _process_record(self, record: OutboxRecord) -> None:
        channel_type = _parse_channel(record.channel)
        if channel_type is None:
            await self._fail_permanently(record, f"Unknown channel: {record.channel}")
            return

        channel = self._channels.get(channel_type)
        if channel is None:
            await self._fail_permanently(record, f"No adapter registered for channel: {record.channel}")
            return

        trigger_event = _parse_trigger(record.trigger_event)
        if trigger_event is None:
            await self._fail_permanently(record, f"Unknown trigger_event: {record.trigger_event!r}")
            return

        contact = ChannelContact(
            channel=channel_type,
            contact_id=record.recipient_address,
            user_id=record.user_id,
            email=record.recipient_email,
            role=record.recipient_role,
        )

        try:
            result = await channel.send(
                contact=contact,
                trigger_event=trigger_event,
                template_data=record.template_context,
            )
        except Exception as exc:
            logger.exception("Channel send raised unexpectedly", id=record.id, channel=record.channel)
            await self._schedule_retry(record, str(exc))
            return

        if result.success:
            await self._repository.mark_delivered(record.id)
            metrics.DELIVERIES_TOTAL.labels(
                channel=record.channel,
                trigger=record.trigger_event,
                outcome="delivered",
            ).inc()
            logger.info("Outbox record delivered", id=record.id, channel=record.channel)
            await self._result_publisher.publish_delivered(record, result.message_id)
            return

        if not result.retryable:
            await self._fail_permanently(record, result.error or "permanent delivery failure")
            return

        await self._schedule_retry(record, result.error)

    async def _fail_permanently(self, record: OutboxRecord, error: str) -> None:
        metrics.DELIVERIES_TOTAL.labels(channel=record.channel, trigger=record.trigger_event, outcome="failed").inc()
        logger.error("Outbox record failed permanently", id=record.id, channel=record.channel, error=error)
        await self._repository.mark_failed(record.id, error=error)

    async def _schedule_retry(self, record: OutboxRecord, error: str | None) -> None:
        next_retry = record.retry_count + 1
        if next_retry > record.max_retries:
            metrics.DELIVERIES_TOTAL.labels(
                channel=record.channel,
                trigger=record.trigger_event,
                outcome="failed",
            ).inc()
            await self._repository.mark_failed(record.id, error=f"retries exhausted: {error}")
            logger.error(
                "Outbox record failed after max retries",
                id=record.id,
                channel=record.channel,
                error=error,
            )
            return
        metrics.DELIVERIES_TOTAL.labels(channel=record.channel, trigger=record.trigger_event, outcome="retried").inc()
        delay = _retry_delay_seconds(next_retry)
        await self._repository.mark_retry(
            record_id=record.id,
            retry_count=next_retry,
            delay_seconds=delay,
            error=error,
        )
        logger.warning(
            "Outbox record send failed, scheduling retry",
            id=record.id,
            channel=record.channel,
            retry=next_retry,
            delay_seconds=delay,
            error=error,
        )


def _parse_channel(channel_str: str) -> ChannelType | None:
    try:
        return ChannelType(channel_str)
    except ValueError:
        return None


def _parse_trigger(trigger_str: str) -> TriggerEvent | None:
    try:
        return TriggerEvent(trigger_str)
    except ValueError:
        return None
