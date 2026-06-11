"""Push notification channel via FCM HTTP v1 API.

Wired but NOT registered in the IoC container (FCM credentials pending).
Kept aligned with the email/telegram retryable-classification contract.
"""

from typing import Any, Protocol

import httpx
import structlog
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, HTTPStatusError

from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

logger = structlog.get_logger(__name__)

_PUSH_TITLES: dict[TriggerEvent, str] = {
    TriggerEvent.BOOKING_CREATED: "Новая встреча",
    TriggerEvent.BOOKING_CANCELLED: "Встреча отменена",
    TriggerEvent.BOOKING_RESCHEDULED: "Встреча перенесена",
    TriggerEvent.BOOKING_REASSIGNED: "Встреча переназначена",
    TriggerEvent.BOOKING_REMINDER: "Напоминание",
    TriggerEvent.BOOKING_REJECTED: "Бронирование отклонено",
}

_RETRYABLE_STATUS_CODES = frozenset({408, 429})


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500


class IAccessTokenProvider(Protocol):
    async def get_access_token(self) -> str: ...


class PushChannel:
    def __init__(
        self,
        *,
        http_client: AsyncClient,
        project_id: str,
        access_token_provider: IAccessTokenProvider,
    ) -> None:
        self._client = http_client
        self._project_id = project_id
        self._token_provider = access_token_provider

    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        title = _PUSH_TITLES.get(trigger_event)
        if title is None:
            return DeliveryResult(
                channel=ChannelType.PUSH,
                success=False,
                retryable=False,
                error=f"No push template for trigger_event={trigger_event.value}",
            )
        access_token = await self._token_provider.get_access_token()

        payload = {
            "message": {
                "token": contact.contact_id,
                "notification": {"title": title, "body": str(template_data.get("body", ""))},
                "data": {"trigger_event": trigger_event.value, **{k: str(v) for k, v in template_data.items()}},
            }
        }

        try:
            response = await self._client.post(
                f"/v1/projects/{self._project_id}/messages:send",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
        except HTTPStatusError as exc:
            error = f"FCM HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            retryable = _is_retryable_status(exc.response.status_code)
            logger.warning("Push send failed", device_token=contact.contact_id[:20], error=error, retryable=retryable)
            return DeliveryResult(channel=ChannelType.PUSH, success=False, error=error, retryable=retryable)
        except httpx.HTTPError as exc:
            logger.warning("Push send transport error", device_token=contact.contact_id[:20], error=str(exc))
            return DeliveryResult(channel=ChannelType.PUSH, success=False, error=str(exc), retryable=True)

        message_name = response.json().get("name", "")
        logger.info("Push sent", device_token=contact.contact_id[:20], message=message_name)
        return DeliveryResult(channel=ChannelType.PUSH, success=True, message_id=message_name)
