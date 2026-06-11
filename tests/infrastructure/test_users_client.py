"""Tests for UsersClient against the real event-users route contract."""

import httpx
import pytest
import respx
from httpx import AsyncClient, Response

from event_notifier.domain.models.notification import UserContacts
from event_notifier.infrastructure.users_client import UsersClient
from event_notifier.interfaces.users_client import UsersServiceError


@pytest.fixture
async def client():
    async with AsyncClient(base_url="http://users-service") as http_client:
        yield UsersClient(http_client=http_client, api_token="test-token")


def user_response(contacts: list[dict] | None = None) -> dict:
    return {
        "id": "uuid-1",
        "email": "org@example.com",
        "name": "Org",
        "role": "organizer",
        "time_zone": "UTC",
        "contacts": contacts or [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


async def test_calls_canonical_by_id_route(client):
    """Regression: the route is /api/users/id/{user_id}, NOT /users/{user_id}."""
    with respx.mock:
        route = respx.get("http://users-service/api/users/id/uuid-1").mock(
            return_value=Response(200, json=user_response())
        )

        contacts = await client.get_user_contacts(user_id="uuid-1")

    assert route.called
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"
    assert contacts == UserContacts(email="org@example.com", telegram_chat_id=None)


async def test_parses_telegram_contact(client):
    with respx.mock:
        respx.get("http://users-service/api/users/id/uuid-1").mock(
            return_value=Response(
                200,
                json=user_response(
                    contacts=[
                        {
                            "id": "c1",
                            "user_id": "uuid-1",
                            "channel": "telegram",
                            "contact_id": "123456789",
                            "created_at": "2026-01-01T00:00:00Z",
                            "updated_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                ),
            )
        )

        contacts = await client.get_user_contacts(user_id="uuid-1")

    assert contacts.telegram_chat_id == "123456789"


async def test_404_returns_none(client):
    with respx.mock:
        respx.get("http://users-service/api/users/id/gone").mock(return_value=Response(404))

        assert await client.get_user_contacts(user_id="gone") is None


async def test_5xx_raises_users_service_error(client):
    with respx.mock:
        respx.get("http://users-service/api/users/id/uuid-1").mock(return_value=Response(503))

        with pytest.raises(UsersServiceError):
            await client.get_user_contacts(user_id="uuid-1")


async def test_auth_failure_raises_users_service_error(client):
    """401/403 means broken config — must be retried/alerted, never treated as 'no user'."""
    with respx.mock:
        respx.get("http://users-service/api/users/id/uuid-1").mock(return_value=Response(401))

        with pytest.raises(UsersServiceError):
            await client.get_user_contacts(user_id="uuid-1")


async def test_timeout_raises_users_service_error(client):
    with respx.mock:
        respx.get("http://users-service/api/users/id/uuid-1").mock(side_effect=httpx.ConnectTimeout("timeout"))

        with pytest.raises(UsersServiceError):
            await client.get_user_contacts(user_id="uuid-1")
