"""Use case: process a domain event and write notification records to outbox."""

from typing import Any

import structlog

from event_notifier.domain.models.notification import DomainEvent
from event_notifier.domain.services.routing import apply_routing_rules
from event_notifier.interfaces.repository import INotificationRepository
from event_notifier.interfaces.users_client import IUsersClient

logger = structlog.get_logger(__name__)


class ProcessDomainEventUseCase:
    def __init__(
        self,
        *,
        repository: INotificationRepository,
        users_client: IUsersClient,
    ) -> None:
        self._repository = repository
        self._users_client = users_client

    async def execute(self, event: DomainEvent) -> None:
        # Idempotency: skip if already processed
        if await self._repository.is_processed(event.event_id):
            logger.info("Event already processed, skipping", event_id=event.event_id)
            return

        # Get routing rules from DB
        routing_rules = await self._repository.get_routing_rules(event.event_type)
        if not routing_rules:
            logger.warning(
                "No routing rules for event type, skipping",
                event_type=event.event_type,
                event_id=event.event_id,
            )
            return

        # Extract (user_id, role) pairs from event data using routing rules
        recipients = apply_routing_rules(
            event_type=event.event_type,
            event_data=event.data,
            routing_rules=routing_rules,
        )
        if not recipients:
            logger.warning(
                "No recipients resolved from event data",
                event_type=event.event_type,
                event_id=event.event_id,
            )
            return

        logger.info(
            "Processing domain event",
            event_type=event.event_type,
            event_id=event.event_id,
            booking_id=event.booking_id,
            recipient_count=len(recipients),
        )

        # Resolve channel contacts for each recipient UUID
        outbox_records: list[dict[str, Any]] = []
        for user_id, role in recipients:
            contacts = await self._users_client.get_contacts_by_id(user_id=user_id, role=role)
            if not contacts:
                logger.warning(
                    "No contacts resolved for user, skipping",
                    user_id=user_id,
                    event_id=event.event_id,
                )
                continue
            for contact in contacts:
                outbox_records.append(
                    {
                        "idempotency_key": f"{event.event_id}:{contact.user_id}:{contact.channel.value}",
                        "cloud_event_id": event.event_id,
                        "booking_id": event.booking_id,
                        "user_id": contact.user_id,
                        "recipient_address": contact.contact_id,
                        "recipient_role": contact.role,
                        "channel": contact.channel.value,
                        "event_type": event.event_type,
                        "template_context": event.data,
                    }
                )

        if not outbox_records:
            logger.warning("No outbox records to write", event_id=event.event_id)
            return

        # Write all outbox records + mark event as processed in one transaction
        await self._repository.write_outbox_atomically(
            cloud_event_id=event.event_id,
            records=outbox_records,
        )
        logger.info(
            "Outbox written",
            event_id=event.event_id,
            records_count=len(outbox_records),
        )
