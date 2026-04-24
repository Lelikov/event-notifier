"""HTTP client for event-users service."""

import httpx
import structlog
from httpx import AsyncClient

from event_notifier.domain.models.notification import ChannelContact, ChannelType

logger = structlog.get_logger(__name__)

_CHANNEL_MAP = {
    "telegram": ChannelType.TELEGRAM,
    "push": ChannelType.PUSH,
}


class UsersClient:
    def __init__(self, *, http_client: AsyncClient, api_token: str) -> None:
        self._client = http_client
        self._headers = {"Authorization": f"Bearer {api_token}"}

    async def get_contacts_by_email(self, *, email: str, role: str) -> list[ChannelContact]:
        """Resolve all notification contacts for a recipient by email.

        Always includes the email channel. Adds telegram/push if found in user_contacts.
        Falls back to email-only on any error.
        """
        contacts: list[ChannelContact] = [
            ChannelContact(
                channel=ChannelType.EMAIL,
                contact_id=email,
                user_id=email,  # legacy: use email as user_id when no UUID available
                role=role,
            )
        ]

        try:
            response = await self._client.get(
                "/api/users",
                params={"email": email, "role": role, "limit": 1, "offset": 0},
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            logger.warning("Failed to fetch user contacts, email-only fallback", email=email)
            return contacts

        items = data.get("items", [])
        if not items:
            logger.debug("User not found in event-users, email-only", email=email)
            return contacts

        for raw_contact in items[0].get("contacts", []):
            channel_str = raw_contact.get("channel", "")
            channel = _CHANNEL_MAP.get(channel_str)
            if channel is None:
                continue
            contacts.append(
                ChannelContact(
                    channel=channel,
                    contact_id=raw_contact["contact_id"],
                    user_id=email,  # legacy: use email as user_id when no UUID available
                    role=role,
                )
            )

        logger.debug("Resolved contacts by email", email=email, channel_count=len(contacts))
        return contacts

    async def get_contacts_by_id(self, *, user_id: str, role: str) -> list[ChannelContact]:
        """Resolve all notification contacts for a recipient by UUID.

        Calls GET /users/{user_id} on event-users service.
        Returns email + telegram/push contacts if available.
        Returns empty list if user not found (404).
        Raises on 5xx/timeout/connection errors so FastStream can nack and retry.
        """
        try:
            response = await self._client.get(
                f"/users/{user_id}",
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug("User not found by id (404)", user_id=user_id)
                return []
            logger.error("Server error fetching user profile by id", user_id=user_id, status=exc.response.status_code)
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.error("Connection/timeout error fetching user profile by id", user_id=user_id, error=str(exc))
            raise

        contacts: list[ChannelContact] = []

        email = data.get("email")
        if email and isinstance(email, str):
            contacts.append(
                ChannelContact(
                    channel=ChannelType.EMAIL,
                    contact_id=email,
                    user_id=user_id,
                    role=role,
                )
            )

        telegram_chat_id = data.get("telegram_chat_id")
        if telegram_chat_id and isinstance(telegram_chat_id, str):
            contacts.append(
                ChannelContact(
                    channel=ChannelType.TELEGRAM,
                    contact_id=telegram_chat_id,
                    user_id=user_id,
                    role=role,
                )
            )

        logger.debug("Resolved contacts by id", user_id=user_id, channel_count=len(contacts))
        return contacts
