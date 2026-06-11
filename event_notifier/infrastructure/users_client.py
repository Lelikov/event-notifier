"""HTTP client for event-users service."""

import httpx
import structlog
from httpx import AsyncClient

from event_notifier.domain.models.notification import UserContacts
from event_notifier.interfaces.users_client import UsersServiceError

logger = structlog.get_logger(__name__)


class UsersClient:
    def __init__(self, *, http_client: AsyncClient, api_token: str) -> None:
        self._client = http_client
        self._headers = {"Authorization": f"Bearer {api_token}"}

    async def get_user_contacts(self, *, user_id: str) -> UserContacts | None:
        """GET /api/users/id/{user_id} → UserContacts, or None when the user is gone (404).

        Any non-404 failure raises UsersServiceError: the message must be
        retried, not ACKed, otherwise the notification is silently lost.
        """
        try:
            response = await self._client.get(f"/api/users/id/{user_id}", headers=self._headers)
        except httpx.HTTPError as exc:
            logger.error("Transport error fetching user from event-users", user_id=user_id, error=str(exc))
            raise UsersServiceError(f"event-users request failed for {user_id}: {exc}") from exc

        if response.status_code == 404:
            logger.warning("User not found in event-users (404)", user_id=user_id)
            return None
        if response.status_code >= 400:
            logger.error(
                "event-users returned error status",
                user_id=user_id,
                status=response.status_code,
            )
            raise UsersServiceError(f"event-users returned {response.status_code} for {user_id}")

        data = response.json()
        telegram_chat_id = _first_contact(data.get("contacts", []), channel="telegram")
        email = data.get("email")
        contacts = UserContacts(
            email=email if isinstance(email, str) and email else None,
            telegram_chat_id=telegram_chat_id,
        )
        logger.debug("Resolved user contacts", user_id=user_id, has_telegram=bool(telegram_chat_id))
        return contacts


def _first_contact(raw_contacts: list, *, channel: str) -> str | None:
    for raw in raw_contacts:
        if not isinstance(raw, dict):
            continue
        if raw.get("channel") != channel:
            continue
        contact_id = raw.get("contact_id")
        if isinstance(contact_id, str) and contact_id:
            return contact_id
    return None
