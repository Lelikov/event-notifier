"""RabbitMQ consumer for the events.notification.commands queue."""

import structlog
from cloudevents.v1.http import from_http
from event_schemas.attributes import BOOKING_ID_ATTRIBUTE
from event_schemas.envelope import EnvelopeParticipant, EventEnvelope
from event_schemas.notification import NotificationCommandPayload
from event_schemas.queues import EVENTS_DLX, NOTIFICATION_COMMANDS_QUEUE, QueueSpec
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from pydantic import ValidationError

from event_notifier.application.use_cases.process_domain_event import ProcessDomainEventUseCase
from event_notifier.domain.models.notification import DomainEvent
from event_notifier.event_types import DOMAIN_EVENT_TO_TRIGGER, NOTIFICATION_COMMAND_EVENT

logger = structlog.get_logger(__name__)


def _resolve_recipients(
    payload: NotificationCommandPayload,
    participants: list[EnvelopeParticipant],
) -> list[dict[str, str]]:
    """Merge command recipients ({email, role}) with receiver-resolved user_ids from the envelope."""
    user_id_by_email = {p.email.lower(): p.user_id for p in participants if p.user_id}
    recipients: list[dict[str, str]] = []
    for recipient in payload.recipients:
        resolved: dict[str, str] = {"email": recipient.email, "role": recipient.role.value}
        user_id = user_id_by_email.get(recipient.email.lower())
        if user_id:
            resolved["user_id"] = user_id
        recipients.append(resolved)
    return recipients


class NotificationConsumer:
    def __init__(
        self,
        *,
        broker: RabbitBroker,
        exchange: RabbitExchange,
        use_case: ProcessDomainEventUseCase,
        queue_spec: QueueSpec = NOTIFICATION_COMMANDS_QUEUE,
    ) -> None:
        self._broker = broker
        self._exchange = exchange
        self._queue_spec = queue_spec
        self._use_case = use_case
        self._started = False

    async def start(self) -> None:
        if self._started:
            return

        queue = RabbitQueue(
            name=self._queue_spec.name,
            durable=True,
            routing_key=str(self._queue_spec.binding),
            declare=True,
            arguments=self._queue_spec.arguments,
        )
        # TODO: Set explicit ack_policy on subscriber if FastStream adds support for it

        @self._broker.subscriber(queue=queue, exchange=self._exchange)
        async def handle(body: bytes, headers: dict) -> None:
            await self._handle(body=body, headers=headers)

        await self._broker.start()
        await self._ensure_dead_letter_topology()
        self._started = True
        logger.info("Notification consumer started", queue=self._queue_spec.name)

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
        await self._broker.close()
        self._started = False
        logger.info("Notification consumer stopped", queue=self._queue_spec.name)

    async def _handle(self, *, body: bytes, headers: dict) -> None:
        try:
            ce = from_http(headers=headers, data=body)
        except Exception:
            logger.exception("Failed to parse CloudEvent")
            raise

        event_type = ce["type"]
        if event_type not in DOMAIN_EVENT_TO_TRIGGER and event_type != NOTIFICATION_COMMAND_EVENT:
            logger.warning("Unknown event type, skipping", event_type=event_type)
            return

        # Unwrap the canonical {original, normalized} envelope produced by event-receiver
        envelope = EventEnvelope.model_validate(ce.data or {})
        data = dict(envelope.original)
        booking_id = ce.get(BOOKING_ID_ATTRIBUTE) or data.get("booking_id", "")

        if event_type == NOTIFICATION_COMMAND_EVENT:
            try:
                payload = envelope.parse_payload(NotificationCommandPayload)
            except ValidationError:
                # Contract violation: dead-letter it (visible in DLQ) instead of silently dropping
                logger.exception("Invalid notification.send_requested payload", event_id=ce["id"])
                raise
            data["recipients"] = _resolve_recipients(payload, envelope.normalized.participants)

        event = DomainEvent(
            event_id=ce["id"],
            event_type=event_type,
            source=ce["source"],
            booking_id=booking_id,
            data=data,
        )

        logger.info(
            "Received domain event",
            event_type=event_type,
            event_id=ce["id"],
            booking_id=booking_id,
        )

        await self._use_case.execute(event)
