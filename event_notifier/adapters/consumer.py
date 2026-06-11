"""RabbitMQ consumer for the events.notification.commands queue.

Ack policy (explicit, exception-driven):
- CloudEvent parse failure / payload contract violation → RejectMessage
  (poison, dead-lettered to events.notification.commands.dlq).
- Transient infrastructure failure (event-users outage, DB connectivity) →
  in-process retries with backoff, then NackMessage(requeue=True) so the
  message is redelivered instead of being lost.
- Anything else unexpected → RejectMessage (poison, visible in the DLQ).
"""

import asyncio
from typing import Any

import structlog
from cloudevents.v1.http import from_http
from event_schemas.attributes import BOOKING_ID_ATTRIBUTE
from event_schemas.envelope import EnvelopeParticipant, EventEnvelope
from event_schemas.notification import NotificationCommandPayload
from event_schemas.queues import EVENTS_DLX, NOTIFICATION_COMMANDS_QUEUE, QueueSpec
from event_schemas.types import EventType
from faststream import Context
from faststream.exceptions import NackMessage, RejectMessage
from faststream.rabbit import Channel, ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlTimeoutError

from event_notifier.application.use_cases.process_notification_command import ProcessNotificationCommandUseCase
from event_notifier.domain.models.notification import CommandRecipient, NotificationCommand
from event_notifier.interfaces.users_client import UsersServiceError

logger = structlog.get_logger(__name__)

DEFAULT_TRANSIENT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5

_TRANSIENT_ERROR_TYPES = (
    UsersServiceError,  # event-users 5xx/auth/transport
    OperationalError,  # DB connectivity / restart
    InterfaceError,  # driver-level connection failure
    SqlTimeoutError,  # connection pool exhaustion
    OSError,  # network errors (includes ConnectionError)
    TimeoutError,  # asyncio timeouts
)


def _is_transient(exc: BaseException) -> bool:
    """Classify an exception as retryable infrastructure failure (vs poison message)."""
    if isinstance(exc, _TRANSIENT_ERROR_TYPES):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def parse_notification_command(*, headers: dict, body: bytes) -> NotificationCommand | None:
    """Parse a binary-mode CloudEvent into a NotificationCommand.

    Returns None for event types this service does not handle (logged + ACKed).
    Raises ValueError/ValidationError for malformed payloads (poison).
    """
    ce = from_http(headers=headers, data=body)

    event_type = ce["type"]
    if event_type != EventType.NOTIFICATION_SEND_REQUESTED.value:
        logger.warning("Unexpected event type on commands queue, acking", event_type=event_type, event_id=ce["id"])
        return None

    envelope = EventEnvelope.model_validate(ce.data or {})
    payload = envelope.parse_payload(NotificationCommandPayload)

    booking_id = ce.get(BOOKING_ID_ATTRIBUTE) or payload.booking_id
    recipients = _resolve_recipients(payload, envelope.normalized.participants)
    # D6: template_context = template_data merged over original (never the wrapper)
    template_context = {**envelope.original, **payload.template_data}

    return NotificationCommand(
        event_id=ce["id"],
        booking_id=booking_id,
        trigger_event=payload.trigger_event.value,
        recipients=recipients,
        template_context=template_context,
    )


def _resolve_recipients(
    payload: NotificationCommandPayload,
    participants: list[EnvelopeParticipant],
) -> tuple[CommandRecipient, ...]:
    """Merge command recipients ({email, role}) with receiver-resolved user_ids/time zones from the envelope."""
    by_email = {p.email.lower(): p for p in participants}
    return tuple(
        CommandRecipient(
            email=recipient.email,
            role=recipient.role.value,
            user_id=_participant_field(by_email.get(recipient.email.lower()), "user_id"),
            time_zone=_participant_field(by_email.get(recipient.email.lower()), "time_zone"),
        )
        for recipient in payload.recipients
    )


def _participant_field(participant: EnvelopeParticipant | None, field: str) -> str | None:
    if participant is None:
        return None
    return getattr(participant, field) or None


class NotificationConsumer:
    def __init__(
        self,
        *,
        broker: RabbitBroker,
        exchange: RabbitExchange,
        use_case: ProcessNotificationCommandUseCase,
        queue_spec: QueueSpec = NOTIFICATION_COMMANDS_QUEUE,
        prefetch_count: int = 10,
        transient_retry_attempts: int = DEFAULT_TRANSIENT_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._broker = broker
        self._exchange = exchange
        self._queue_spec = queue_spec
        self._use_case = use_case
        self._prefetch_count = prefetch_count
        self._transient_retry_attempts = transient_retry_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def build_queue_and_channel(self) -> tuple[RabbitQueue, Channel]:
        """Canonical queue declaration (event_schemas.queues) + QoS channel."""
        queue = RabbitQueue(
            name=self._queue_spec.name,
            durable=True,
            routing_key=str(self._queue_spec.binding),
            declare=True,
            arguments=self._queue_spec.arguments,
        )
        return queue, Channel(prefetch_count=self._prefetch_count)

    async def start(self) -> None:
        if self._started:
            return

        queue, channel = self.build_queue_and_channel()
        subscriber = self._broker.subscriber(queue=queue, exchange=self._exchange, channel=channel)
        subscriber(self._make_handler())

        await self._broker.start()
        await self._ensure_dead_letter_topology()
        self._started = True
        logger.info("Notification consumer started", queue=self._queue_spec.name, prefetch=self._prefetch_count)

    def _make_handler(self):
        """Handler takes the raw message via Context, so FastStream never tries to
        pydantic-validate handler parameters against the decoded body."""

        async def consume(message: Any = Context("message")) -> None:  # noqa: B008
            await self._consume_message(message)

        return consume

    async def _ensure_dead_letter_topology(self) -> None:
        """Idempotently declare the DLX and own DLQ (no startup-order dependency on event-receiver)."""
        dlx = RabbitExchange(name=EVENTS_DLX, type=ExchangeType.TOPIC, durable=True)
        declared_dlx = await self._broker.declare_exchange(dlx)
        dlq = RabbitQueue(
            name=self._queue_spec.dlq_name,
            durable=True,
            routing_key=self._queue_spec.dlq_name,
            arguments=self._queue_spec.dlq_arguments,
        )
        declared_dlq = await self._broker.declare_queue(dlq)
        await declared_dlq.bind(exchange=declared_dlx, routing_key=self._queue_spec.dlq_name)
        logger.info("Dead-letter topology ensured", dlx=EVENTS_DLX, dlq=self._queue_spec.dlq_name)

    async def stop(self) -> None:
        if not self._started:
            return
        await self._broker.stop()
        self._started = False
        logger.info("Notification consumer stopped", queue=self._queue_spec.name)

    async def _consume_message(self, message: Any) -> None:
        try:
            command = parse_notification_command(headers=dict(message.headers), body=message.body)
        except Exception as exc:
            logger.exception("Poison message (unparseable CloudEvent or invalid payload), dead-lettering")
            raise RejectMessage from exc

        if command is None:
            return

        logger.info(
            "Received notification command",
            event_id=command.event_id,
            booking_id=command.booking_id,
            trigger_event=command.trigger_event,
            recipient_count=len(command.recipients),
        )
        await self._execute_with_retry(command)

    async def _execute_with_retry(self, command: NotificationCommand) -> None:
        last_error: BaseException | None = None
        for attempt in range(1, self._transient_retry_attempts + 1):
            last_error = await self._attempt_execute(command)
            if last_error is None:
                return
            logger.warning(
                "Transient failure processing notification command, retrying",
                attempt=attempt,
                max_attempts=self._transient_retry_attempts,
                error=str(last_error),
                event_id=command.event_id,
            )
            if attempt < self._transient_retry_attempts:
                await asyncio.sleep(self._retry_backoff_seconds * 2 ** (attempt - 1))

        logger.error("Transient retries exhausted, requeueing message", event_id=command.event_id)
        raise NackMessage(requeue=True) from last_error

    async def _attempt_execute(self, command: NotificationCommand) -> BaseException | None:
        """Run one attempt; return the transient error, reject poison, None on success."""
        try:
            await self._use_case.execute(command)
        except NackMessage, RejectMessage:
            raise
        except Exception as exc:
            if not _is_transient(exc):
                logger.exception("Non-transient failure processing command: poison, dead-lettering")
                raise RejectMessage from exc
            return exc
        return None
