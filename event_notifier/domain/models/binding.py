from dataclasses import dataclass

from event_notifier.domain.models.notification import ChannelType


@dataclass(frozen=True)
class NotificationBinding:
    trigger_event: str
    recipient_role: str  # "organizer" | "client" (RecipientRole value)
    channel: ChannelType
    enabled: bool
    unisender_template_id: str | None
    telegram_body: str | None
