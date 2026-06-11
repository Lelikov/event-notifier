from typing import Protocol

from event_notifier.domain.models.notification import UserContacts


class UsersServiceError(Exception):
    """Transport/server failure talking to event-users — transient, retry the message."""


class IUsersClient(Protocol):
    async def get_user_contacts(self, *, user_id: str) -> UserContacts | None:
        """Resolve a user's channel contacts by event-users UUID.

        Returns None when the user does not exist (404).
        Raises UsersServiceError on any other failure (5xx, auth, transport,
        timeout) so the caller can NACK and retry instead of silently dropping.
        """
        ...
