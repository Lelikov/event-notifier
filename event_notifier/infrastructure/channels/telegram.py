"""Telegram notification channel via Bot API sendMessage.

Message bodies are Jinja2 templates (templates/<locale>/telegram/<TRIGGER_EVENT>.j2),
rendered with the flat template_data — never hardcoded strings, never the raw
trigger name leaked to end users. The template language is chosen by
``template_data["locale"]`` with fallback to the configured default locale;
unknown triggers (no template in any candidate locale) fail permanently.
"""

from typing import Any

import httpx
import structlog
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, HTTPStatusError
from jinja2 import Environment, TemplateNotFound

from event_notifier.domain.localization import normalize_locale
from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

logger = structlog.get_logger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({408, 429})


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500


class TelegramChannel:
    def __init__(
        self,
        *,
        http_client: AsyncClient,
        bot_token: str,
        template_env: Environment,
        default_locale: str = "ru",
    ) -> None:
        self._client = http_client
        self._bot_token = bot_token
        self._env = template_env
        self._default_locale = default_locale

    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        text = self._render(trigger_event, template_data)
        if text is None:
            return DeliveryResult(
                channel=ChannelType.TELEGRAM,
                success=False,
                retryable=False,
                error=f"No telegram template for trigger_event={trigger_event.value}",
            )

        try:
            response = await self._client.post(
                f"/bot{self._bot_token}/sendMessage",
                json={"chat_id": contact.contact_id, "text": text, "parse_mode": "HTML"},
            )
            response.raise_for_status()
        except HTTPStatusError as exc:
            error = f"Telegram HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            retryable = _is_retryable_status(exc.response.status_code)
            logger.warning("Telegram send failed", chat_id=contact.contact_id, error=error, retryable=retryable)
            return DeliveryResult(channel=ChannelType.TELEGRAM, success=False, error=error, retryable=retryable)
        except httpx.HTTPError as exc:
            logger.warning("Telegram send transport error", chat_id=contact.contact_id, error=str(exc))
            return DeliveryResult(channel=ChannelType.TELEGRAM, success=False, error=str(exc), retryable=True)

        body = response.json()
        message_id = str(body.get("result", {}).get("message_id", ""))
        logger.info("Telegram message sent", chat_id=contact.contact_id, message_id=message_id)
        return DeliveryResult(channel=ChannelType.TELEGRAM, success=True, message_id=message_id)

    def _render(self, trigger_event: TriggerEvent, template_data: dict[str, Any]) -> str | None:
        """Render the trigger's template in the recipient's locale, falling back to the default locale."""
        locale = normalize_locale(template_data.get("locale")) or self._default_locale
        for candidate in dict.fromkeys((locale, self._default_locale)):
            try:
                template = self._env.get_template(f"{candidate}/telegram/{trigger_event.value}.j2")
            except TemplateNotFound:
                continue
            return template.render(**template_data).strip()
        return None
