"""Email notification channel via UniSender Go transactional API.

External contract (hard invariant): POST /ru/transactional/api/v1/email/send.json
with the API key in the X-API-KEY header (never in the body, never logged) and
``message.template_id`` set to a real template UUID provisioned in UniSender Go
and supplied via the notification_bindings table (admin-managed).
"""

from typing import Any

import httpx
import structlog
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, HTTPStatusError
from opentelemetry import trace

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

_tracer = trace.get_tracer(__name__)

logger = structlog.get_logger(__name__)

UNISENDER_SEND_PATH = "/ru/transactional/api/v1/email/send.json"

_RETRYABLE_STATUS_CODES = frozenset({408, 429})


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500


def flatten_substitutions(template_data: dict[str, Any]) -> dict[str, str]:
    """Only scalar key/values reach global_substitutions.

    Drops nested structures (the recipients list, dicts) so templates always get
    flat values and no PII-bearing collections leak into the provider payload.
    """
    return {k: str(v) for k, v in template_data.items() if isinstance(v, str | int | float | bool)}


class EmailChannel:
    def __init__(
        self,
        *,
        http_client: AsyncClient,
        bindings: BindingsProvider,
        from_email: str,
        from_name: str,
    ) -> None:
        self._client = http_client
        self._bindings = bindings
        self._from_email = from_email
        self._from_name = from_name

    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        with _tracer.start_as_current_span("notifier.channel_send") as span:
            span.set_attribute("channel", "email")
            template_id = await self._template_id(trigger_event, contact.role)
            if not template_id:
                return DeliveryResult(
                    channel=ChannelType.EMAIL,
                    success=False,
                    retryable=False,
                    error=f"No UniSender template configured for trigger_event={trigger_event.value}",
                )

            payload = {
                "message": {
                    "template_id": template_id,
                    "recipients": [{"email": contact.contact_id}],
                    "from_email": self._from_email,
                    "from_name": self._from_name,
                    "global_substitutions": flatten_substitutions(template_data),
                },
            }

            try:
                response = await self._client.post(UNISENDER_SEND_PATH, json=payload)
                response.raise_for_status()
            except HTTPStatusError as exc:
                error = f"UniSender HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                retryable = _is_retryable_status(exc.response.status_code)
                logger.warning("Email send failed", to=contact.contact_id, error=error, retryable=retryable)
                return DeliveryResult(channel=ChannelType.EMAIL, success=False, error=error, retryable=retryable)
            except httpx.HTTPError as exc:
                logger.warning("Email send transport error", to=contact.contact_id, error=str(exc))
                return DeliveryResult(channel=ChannelType.EMAIL, success=False, error=str(exc), retryable=True)

            body = response.json()
            job_id = body.get("job_id")
        logger.info("Email sent", to=contact.contact_id, trigger=trigger_event.value, job_id=job_id)
        return DeliveryResult(channel=ChannelType.EMAIL, success=True, message_id=job_id)

    async def _template_id(self, trigger_event: TriggerEvent, recipient_role: str) -> str | None:
        binding = await self._bindings.get(trigger_event.value, recipient_role, ChannelType.EMAIL)
        if binding is None or not binding.enabled:
            return None
        return binding.unisender_template_id
