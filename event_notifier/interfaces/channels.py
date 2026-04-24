from typing import Any, Protocol

from event_notifier.domain.models.notification import ChannelContact, DeliveryResult


class INotificationChannel(Protocol):
    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: str,
        template_data: dict[str, Any],
    ) -> DeliveryResult: ...
