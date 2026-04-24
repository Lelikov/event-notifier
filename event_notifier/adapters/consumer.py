"""RabbitMQ consumer for events.notifications queue (domain events)."""

import structlog
from cloudevents.v1.http import from_http
from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitQueue

from event_notifier.application.use_cases.process_domain_event import ProcessDomainEventUseCase
from event_notifier.domain.models.notification import DomainEvent
from event_notifier.event_types import DOMAIN_EVENT_TO_TRIGGER

logger = structlog.get_logger(__name__)


class NotificationConsumer:
    def __init__(
        self,
        *,
        broker: RabbitBroker,
        exchange: RabbitExchange,
        queue_name: str,
        use_case: ProcessDomainEventUseCase,
    ) -> None:
        self._broker = broker
        self._exchange = exchange
        self._queue_name = queue_name
        self._use_case = use_case
        self._started = False

    async def start(self) -> None:
        if self._started:
            return

        queue = RabbitQueue(
            name=self._queue_name,
            durable=True,
            routing_key=self._queue_name,
            declare=True,
            arguments={"x-dead-letter-exchange": "events.dlx"},
        )
        # TODO: Set explicit ack_policy on subscriber if FastStream adds support for it

        @self._broker.subscriber(queue=queue, exchange=self._exchange)
        async def handle(body: bytes, headers: dict) -> None:
            await self._handle(body=body, headers=headers)

        await self._broker.start()
        self._started = True
        logger.info("Notification consumer started", queue=self._queue_name)

    async def stop(self) -> None:
        if not self._started:
            return
        await self._broker.close()
        self._started = False
        logger.info("Notification consumer stopped", queue=self._queue_name)

    async def _handle(self, *, body: bytes, headers: dict) -> None:
        try:
            ce = from_http(headers=headers, data=body)
        except Exception:
            logger.exception("Failed to parse CloudEvent")
            raise

        event_type = ce["type"]
        if event_type not in DOMAIN_EVENT_TO_TRIGGER:
            logger.warning("Unknown event type, skipping", event_type=event_type)
            return

        booking_id = ce.get("booking_id") or (ce.data or {}).get("booking_id", "")
        data = ce.data or {}

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
