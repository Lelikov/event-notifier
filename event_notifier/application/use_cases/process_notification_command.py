"""Use case: turn a notification.send_requested command into transactional outbox records."""

from typing import Any

import structlog

from event_notifier.domain.localization import localize_template_context
from event_notifier.domain.models.notification import (
    ChannelContact,
    ChannelType,
    CommandRecipient,
    NotificationCommand,
)
from event_notifier.interfaces.repository import INotificationRepository
from event_notifier.interfaces.users_client import IUsersClient

logger = structlog.get_logger(__name__)


class ProcessNotificationCommandUseCase:
    """Resolves recipients to channel contacts and writes the outbox atomically.

    Contact resolution policy:
    - Email is always taken from the command recipient itself (the producer's
      contract guarantees it), so an email delivery is never lost to a failed
      or missing user lookup.
    - When the receiver resolved a user_id (normalized.participants), the
      event-users profile only ADDS extra channels (telegram). A 404 there
      degrades to email-only with a warning; transport/5xx errors propagate
      so the message is NACKed and retried.
    """

    def __init__(
        self,
        *,
        repository: INotificationRepository,
        users_client: IUsersClient,
    ) -> None:
        self._repository = repository
        self._users_client = users_client

    async def execute(self, command: NotificationCommand) -> None:
        if await self._repository.is_processed(command.event_id):
            logger.info("Event already processed, skipping", event_id=command.event_id)
            return

        records: list[dict[str, Any]] = []
        for recipient in command.recipients:
            template_context = localize_template_context(command.template_context, recipient.time_zone)
            for contact in await self._resolve_contacts(recipient):
                records.append(self._to_outbox_record(command, contact, template_context))

        if not records:
            # Explicit no_contacts outcome: still claim the event (no redelivery loop),
            # but make the loss visible as a structured warning, never a silent ack.
            logger.warning(
                "notification command yielded no deliverable contacts",
                outcome="no_contacts",
                event_id=command.event_id,
                booking_id=command.booking_id,
                trigger_event=command.trigger_event,
                recipient_count=len(command.recipients),
            )
            await self._repository.write_outbox_atomically(cloud_event_id=command.event_id, records=[])
            return

        written = await self._repository.write_outbox_atomically(
            cloud_event_id=command.event_id,
            records=records,
        )
        if not written:
            logger.info("Concurrent duplicate, outbox already written", event_id=command.event_id)
            return
        logger.info(
            "Outbox written",
            event_id=command.event_id,
            booking_id=command.booking_id,
            trigger_event=command.trigger_event,
            records_count=len(records),
        )

    async def _resolve_contacts(self, recipient: CommandRecipient) -> list[ChannelContact]:
        contacts = [
            ChannelContact(
                channel=ChannelType.EMAIL,
                contact_id=recipient.email,
                user_id=recipient.user_id or "",
                email=recipient.email,
                role=recipient.role,
            )
        ]
        if not recipient.user_id:
            logger.warning(
                "Recipient has no resolved user_id, email-only delivery",
                email=recipient.email,
                role=recipient.role,
            )
            return contacts

        user_contacts = await self._users_client.get_user_contacts(user_id=recipient.user_id)
        if user_contacts is None:
            logger.warning(
                "User vanished from event-users, email-only delivery",
                user_id=recipient.user_id,
                email=recipient.email,
            )
            return contacts

        if user_contacts.telegram_chat_id:
            contacts.append(
                ChannelContact(
                    channel=ChannelType.TELEGRAM,
                    contact_id=user_contacts.telegram_chat_id,
                    user_id=recipient.user_id,
                    email=recipient.email,
                    role=recipient.role,
                )
            )
        return contacts

    @staticmethod
    def _to_outbox_record(
        command: NotificationCommand,
        contact: ChannelContact,
        template_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            # Keyed by recipient email (stable across user_id backfills), not UUID.
            "idempotency_key": f"{command.event_id}:{contact.email}:{contact.channel.value}",
            "cloud_event_id": command.event_id,
            "booking_id": command.booking_id,
            "user_id": contact.user_id,
            "recipient_email": contact.email,
            "recipient_address": contact.contact_id,
            "recipient_role": contact.role,
            "channel": contact.channel.value,
            "trigger_event": command.trigger_event,
            # Per-recipient: includes *_local time keys when the recipient's zone is known.
            "template_context": template_context,
        }
