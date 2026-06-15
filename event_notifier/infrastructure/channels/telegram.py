"""Telegram notification channel via Bot API sendMessage.

Message bodies are stored in the notification_bindings table (admin-managed)
and rendered with Jinja2 SandboxedEnvironment from the binding's telegram_body.
Unknown triggers (no binding or body) fail permanently.
"""

from typing import Any

import httpx
import structlog
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, HTTPStatusError
from jinja2.sandbox import SandboxedEnvironment
from opentelemetry import trace

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

_tracer = trace.get_tracer(__name__)

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
        bindings: BindingsProvider,
    ) -> None:
        self._client = http_client
        self._bot_token = bot_token
        self._bindings = bindings
        self._jinja = SandboxedEnvironment(autoescape=False)

    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        with _tracer.start_as_current_span("notifier.channel_send") as span:
            span.set_attribute("channel", "telegram")
            text = await self._render(trigger_event, contact.role, template_data)
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

    async def _render(
        self, trigger_event: TriggerEvent, recipient_role: str, template_data: dict[str, Any]
    ) -> str | None:
        binding = await self._bindings.get(trigger_event.value, recipient_role, ChannelType.TELEGRAM)
        if binding is None or not binding.enabled or not binding.telegram_body:
            return None
        return self._jinja.from_string(binding.telegram_body).render(**template_data).strip()
