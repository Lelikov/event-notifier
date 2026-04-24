"""Background outbox sender: polls notification_outbox and delivers via channel adapters."""

import asyncio

import structlog

from event_notifier.domain.models.notification import ChannelContact, ChannelType, OutboxRecord
from event_notifier.event_types import DOMAIN_EVENT_TO_TRIGGER
from event_notifier.interfaces.channels import INotificationChannel
from event_notifier.interfaces.repository import INotificationRepository

logger = structlog.get_logger(__name__)


def _retry_delay_seconds(retry_count: int) -> int:
    """Exponential backoff: 10s, 40s, 90s, 160s, 250s for retries 1–5."""
    return 10 * retry_count**2


class OutboxSender:
    def __init__(
        self,
        *,
        repository: INotificationRepository,
        channels: dict[ChannelType, INotificationChannel],
        batch_size: int = 10,
        poll_interval: float = 1.0,
    ) -> None:
        self._repository = repository
        self._channels = channels
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._running = False

    async def run_once(self) -> None:
        """Process one batch of pending outbox records. Used in tests and the main loop."""
        records = await self._repository.fetch_pending_outbox(self._batch_size)
        for record in records:
            await self._process_record(record)

    async def start(self) -> None:
        self._running = True
        logger.info("OutboxSender started", poll_interval=self._poll_interval)
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("OutboxSender loop error")
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("OutboxSender stopped")

    async def _process_record(self, record: OutboxRecord) -> None:
        channel_type = _parse_channel(record.channel)
        if channel_type is None:
            logger.error("Unknown channel in outbox record, marking failed", channel=record.channel, id=record.id)
            await self._repository.mark_failed(record.id)
            return

        channel = self._channels.get(channel_type)
        if channel is None:
            logger.error("No adapter for channel, marking failed", channel=record.channel, id=record.id)
            await self._repository.mark_failed(record.id)
            return

        trigger_event = DOMAIN_EVENT_TO_TRIGGER.get(record.event_type, record.event_type)
        contact = ChannelContact(
            channel=channel_type,
            contact_id=record.recipient_address,
            user_id=record.user_id,
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
            result_success = False
            result_error = str(exc)
        else:
            result_success = result.success
            result_error = result.error

        if result_success:
            await self._repository.mark_delivered(record.id)
            # TODO: Publish notification.*.message_sent delivery result events
            # back to event-receiver via POST /event/cloudevents
            logger.info("Outbox record delivered", id=record.id, channel=record.channel)
        else:
            next_retry = record.retry_count + 1
            if next_retry > record.max_retries:
                await self._repository.mark_failed(record.id)
                logger.warning(
                    "Outbox record failed after max retries",
                    id=record.id,
                    channel=record.channel,
                    error=result_error,
                )
            else:
                delay = _retry_delay_seconds(next_retry)
                await self._repository.mark_retry(
                    record_id=record.id,
                    retry_count=next_retry,
                    delay_seconds=delay,
                )
                logger.warning(
                    "Outbox record send failed, scheduling retry",
                    id=record.id,
                    channel=record.channel,
                    retry=next_retry,
                    delay_seconds=delay,
                    error=result_error,
                )


def _parse_channel(channel_str: str) -> ChannelType | None:
    try:
        return ChannelType(channel_str)
    except ValueError:
        return None
