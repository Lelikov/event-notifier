"""Email notification channel via UniSender Go transactional API."""

from typing import Any

import structlog
from httpx import AsyncClient, HTTPStatusError

from event_notifier.domain.models.notification import ChannelContact, ChannelType, DeliveryResult

logger = structlog.get_logger(__name__)

# Maps trigger_event → UniSender template code.
_TEMPLATE_MAP: dict[str, str] = {
    "BOOKING_CREATED": "booking_created",
    "BOOKING_CANCELLED": "booking_cancelled",
    "BOOKING_RESCHEDULED": "booking_rescheduled",
    "BOOKING_REASSIGNED": "booking_reassigned",
    "BOOKING_REMINDER": "booking_reminder",
    "BOOKING_REJECTED": "booking_rejected",
}

_UNISENDER_URL = "/ru/transactional/api/v1/email/send.json"


class EmailChannel:
    def __init__(
        self,
        *,
        http_client: AsyncClient,
        api_key: str,
        from_email: str,
        from_name: str,
    ) -> None:
        self._client = http_client
        self._api_key = api_key
        self._from_email = from_email
        self._from_name = from_name

    async def send(
        self,
        *,
        contact: ChannelContact,
        trigger_event: str,
        template_data: dict[str, Any],
    ) -> DeliveryResult:
        template_code = _TEMPLATE_MAP.get(trigger_event)
        if not template_code:
            return DeliveryResult(
                channel=ChannelType.EMAIL,
                success=False,
                error=f"No email template for trigger_event={trigger_event}",
            )

        payload = {
            "api_key": self._api_key,
            "message": {
                "template_id": template_code,
                "recipients": [{"email": contact.contact_id}],
                "from_email": self._from_email,
                "from_name": self._from_name,
                "global_substitutions": template_data,
            },
        }

        try:
            response = await self._client.post(_UNISENDER_URL, json=payload)
            response.raise_for_status()
            body = response.json()
            job_id = body.get("job_id")
            logger.info("Email sent", to=contact.contact_id, trigger=trigger_event, job_id=job_id)
            return DeliveryResult(channel=ChannelType.EMAIL, success=True, message_id=job_id)
        except HTTPStatusError as exc:
            error = f"UniSender HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            logger.warning("Email send failed", to=contact.contact_id, error=error)
            return DeliveryResult(channel=ChannelType.EMAIL, success=False, error=error)
        except Exception as exc:
            logger.exception("Email send unexpected error", to=contact.contact_id)
            return DeliveryResult(channel=ChannelType.EMAIL, success=False, error=str(exc))
