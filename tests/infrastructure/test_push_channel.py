from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import AsyncClient, Response

from event_notifier.domain.models.notification import ChannelContact, ChannelType
from event_notifier.infrastructure.channels.push import PushChannel


@pytest.fixture
async def push_channel():
    token_provider = MagicMock()
    token_provider.get_access_token = AsyncMock(return_value="fake-access-token")
    async with AsyncClient(base_url="https://fcm.googleapis.com") as client:
        yield PushChannel(
            http_client=client,
            project_id="my-project",
            access_token_provider=token_provider,
        )


@pytest.fixture
def contact():
    return ChannelContact(
        channel=ChannelType.PUSH,
        contact_id="device-token-xyz",
        user_id="uuid-client-001",
        role="client",
    )


@pytest.mark.asyncio
async def test_send_push_success(push_channel, contact):
    with respx.mock:
        respx.post("https://fcm.googleapis.com/v1/projects/my-project/messages:send").mock(
            return_value=Response(200, json={"name": "projects/my-project/messages/msg-123"})
        )

        result = await push_channel.send(
            contact=contact,
            trigger_event="BOOKING_CREATED",
            template_data={"booking_id": "b-1"},
        )

    assert result.success is True
    assert "msg-123" in result.message_id


@pytest.mark.asyncio
async def test_send_push_invalid_token(push_channel, contact):
    with respx.mock:
        respx.post("https://fcm.googleapis.com/v1/projects/my-project/messages:send").mock(
            return_value=Response(400, json={"error": {"code": 400, "message": "INVALID_ARGUMENT"}})
        )

        result = await push_channel.send(
            contact=contact,
            trigger_event="BOOKING_CREATED",
            template_data={},
        )

    assert result.success is False
    assert result.error is not None
