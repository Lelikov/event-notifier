from typing import Protocol

from event_notifier.domain.models.notification import ChannelContact


class IUsersClient(Protocol):
    async def get_contacts_by_email(self, *, email: str, role: str) -> list[ChannelContact]: ...

    async def get_contacts_by_id(self, *, user_id: str, role: str) -> list[ChannelContact]: ...
