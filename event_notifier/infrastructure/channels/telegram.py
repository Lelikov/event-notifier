"""Telegram notification channel via Bot API sendMessage."""

from typing import Any

import structlog
from httpx import AsyncClient, HTTPStatusError

from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

logger = structlog.get_logger(__name__)

_MESSAGE_TEMPLATES: dict[str, str] = {
    "BOOKING_CREATED": "Новая встреча забронирована.",
    "BOOKING_CANCELLED": "Встреча отменена.",
    "BOOKING_RESCHEDULED": "Встреча перенесена.",
    "BOOKING_REASSIGNED": "Встреча переназначена.",
    "BOOKING_REMINDER": "Напоминание о встрече.",
    "BOOKING_REJECTED": "Бронирование отклонено.",
}


class TelegramChannel:
    def __init__(self, *, http_client: AsyncClient, bot_token: str) -> None:
        self._client = http_client
        self._bot_token = bot_token

    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: str,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        text = _MESSAGE_TEMPLATES.get(trigger_event, f"Уведомление: {trigger_event}")

        try:
            response = await self._client.post(
                f"/bot{self._bot_token}/sendMessage",
                json={"chat_id": contact.contact_id, "text": text, "parse_mode": "HTML"},
            )
            response.raise_for_status()
            body = response.json()
            message_id = str(body.get("result", {}).get("message_id", ""))
            logger.info("Telegram message sent", chat_id=contact.contact_id, message_id=message_id)
            return DeliveryResult(channel=ChannelType.TELEGRAM, success=True, message_id=message_id)
        except HTTPStatusError as exc:
            error = f"Telegram HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            logger.warning("Telegram send failed", chat_id=contact.contact_id, error=error)
            return DeliveryResult(channel=ChannelType.TELEGRAM, success=False, error=error)
        except Exception as exc:
            logger.exception("Telegram send unexpected error", chat_id=contact.contact_id)
            return DeliveryResult(channel=ChannelType.TELEGRAM, success=False, error=str(exc))
