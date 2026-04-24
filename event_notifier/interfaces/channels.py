from typing import Any, Protocol

from event_schemas.types import TriggerEvent

from event_notifier.domain.models.notification import ChannelContact, DeliveryResult


class INotificationChannel(Protocol):
    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult: ...
