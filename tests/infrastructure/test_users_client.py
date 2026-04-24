import pytest
import respx
from httpx import AsyncClient, Response

from event_notifier.domain.models.notification import ChannelType
from event_notifier.infrastructure.users_client import UsersClient


@pytest.fixture
def http_client():
    return AsyncClient(base_url="http://users-service")


@pytest.mark.asyncio
async def test_get_contacts_by_email_returns_email_and_telegram(http_client):
    with respx.mock:
        respx.get("http://users-service/api/users").mock(
            return_value=Response(
                200,
                json={
                    "items": [
                        {
                            "id": "uuid-1",
                            "email": "org@example.com",
                            "role": "organizer",
                            "name": "Org",
                            "time_zone": "UTC",
                            "contacts": [
                                {
                                    "channel": "telegram",
                                    "contact_id": "123456789",
                                    "id": "c1",
                                    "user_id": "uuid-1",
                                    "created_at": "2026-01-01T00:00:00Z",
                                    "updated_at": "2026-01-01T00:00:00Z",
                                },
                            ],
                            "created_at": "2026-01-01T00:00:00Z",
                            "updated_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            )
        )

        client = UsersClient(http_client=http_client, api_token="token")
        contacts = await client.get_contacts_by_email(email="org@example.com", role="organizer")

    # Email channel всегда добавляется (primary contact)
    email_contacts = [c for c in contacts if c.channel == ChannelType.EMAIL]
    assert len(email_contacts) == 1
    assert email_contacts[0].contact_id == "org@example.com"

    # Telegram из contacts
    tg_contacts = [c for c in contacts if c.channel == ChannelType.TELEGRAM]
    assert len(tg_contacts) == 1
    assert tg_contacts[0].contact_id == "123456789"


@pytest.mark.asyncio
async def test_get_contacts_by_email_user_not_found_returns_email_only(http_client):
    with respx.mock:
        respx.get("http://users-service/api/users").mock(return_value=Response(200, json={"items": []}))

        client = UsersClient(http_client=http_client, api_token="token")
        contacts = await client.get_contacts_by_email(email="unknown@example.com", role="client")

    # Даже если юзер не найден — email-канал всегда доступен
    assert len(contacts) == 1
    assert contacts[0].channel == ChannelType.EMAIL
    assert contacts[0].contact_id == "unknown@example.com"


@pytest.mark.asyncio
async def test_get_contacts_by_id_returns_email_and_telegram(http_client):
    user_id = "550e8400-e29b-41d4-a716-446655440001"
    with respx.mock:
        respx.get(f"http://users-service/users/{user_id}").mock(
            return_value=Response(
                200,
                json={
                    "id": user_id,
                    "role": "volunteer",
                    "first_name": "Ivan",
                    "last_name": "Petrov",
                    "email": "ivan@example.com",
                    "telegram_chat_id": "987654321",
                },
            )
        )

        client = UsersClient(http_client=http_client, api_token="token")
        contacts = await client.get_contacts_by_id(user_id=user_id, role="volunteer")

    email_contacts = [c for c in contacts if c.channel == ChannelType.EMAIL]
    assert len(email_contacts) == 1
    assert email_contacts[0].contact_id == "ivan@example.com"
    assert email_contacts[0].user_id == user_id

    tg_contacts = [c for c in contacts if c.channel == ChannelType.TELEGRAM]
    assert len(tg_contacts) == 1
    assert tg_contacts[0].contact_id == "987654321"


@pytest.mark.asyncio
async def test_get_contacts_by_id_user_not_found_returns_empty(http_client):
    user_id = "unknown-uuid"
    with respx.mock:
        respx.get(f"http://users-service/users/{user_id}").mock(
            return_value=Response(404, json={"detail": "Not found"})
        )

        client = UsersClient(http_client=http_client, api_token="token")
        contacts = await client.get_contacts_by_id(user_id=user_id, role="client")

    assert contacts == []
