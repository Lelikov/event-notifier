"""Push notification channel via FCM HTTP v1 API."""

from typing import Any, Protocol

import structlog
from httpx import AsyncClient, HTTPStatusError

from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

logger = structlog.get_logger(__name__)

_PUSH_TITLES: dict[str, str] = {
    "BOOKING_CREATED": "Новая встреча",
    "BOOKING_CANCELLED": "Встреча отменена",
    "BOOKING_RESCHEDULED": "Встреча перенесена",
    "BOOKING_REASSIGNED": "Встреча переназначена",
    "BOOKING_REMINDER": "Напоминание",
    "BOOKING_REJECTED": "Бронирование отклонено",
}


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
        trigger_event: str,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        title = _PUSH_TITLES.get(trigger_event, "Уведомление")
        access_token = await self._token_provider.get_access_token()

        payload = {
            "message": {
                "token": contact.contact_id,
                "notification": {"title": title, "body": template_data.get("body", "")},
                "data": {"trigger_event": trigger_event, **{k: str(v) for k, v in template_data.items()}},
            }
        }

        try:
            response = await self._client.post(
                f"/v1/projects/{self._project_id}/messages:send",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            message_name = response.json().get("name", "")
            logger.info("Push sent", device_token=contact.contact_id[:20], message=message_name)
            return DeliveryResult(channel=ChannelType.PUSH, success=True, message_id=message_name)
        except HTTPStatusError as exc:
            error = f"FCM HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            logger.warning("Push send failed", device_token=contact.contact_id[:20], error=error)
            return DeliveryResult(channel=ChannelType.PUSH, success=False, error=error)
        except Exception as exc:
            logger.exception("Push send unexpected error", device_token=contact.contact_id[:20])
            return DeliveryResult(channel=ChannelType.PUSH, success=False, error=str(exc))
