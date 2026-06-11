"""Email notification channel via UniSender Go transactional API.

External contract (hard invariant): POST /ru/transactional/api/v1/email/send.json
with the API key in the X-API-KEY header (never in the body, never logged) and
``message.template_id`` set to a real template UUID provisioned in UniSender Go
and supplied via UNISENDER_TEMPLATE_IDS config.

Template ids are locale-keyed ({locale: {TRIGGER_EVENT: id}}); the recipient's
``template_data["locale"]`` selects the set, falling back to the default locale.
"""

from typing import Any

import httpx
import structlog
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, HTTPStatusError

from event_notifier.domain.localization import normalize_locale
from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

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
        template_ids_by_locale: dict[str, dict[str, str]],
        from_email: str,
        from_name: str,
        default_locale: str = "ru",
    ) -> None:
        self._client = http_client
        self._template_ids_by_locale = template_ids_by_locale
        self._from_email = from_email
        self._from_name = from_name
        self._default_locale = default_locale

    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: TriggerEvent,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        template_id = self._template_id(trigger_event, template_data)
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

    def _template_id(self, trigger_event: TriggerEvent, template_data: dict[str, Any]) -> str | None:
        """Pick the template id for the recipient's locale, falling back to the default locale's set."""
        locale = normalize_locale(template_data.get("locale")) or self._default_locale
        for candidate in dict.fromkeys((locale, self._default_locale)):
            template_id = self._template_ids_by_locale.get(candidate, {}).get(trigger_event.value)
            if template_id:
                return template_id
        return None
