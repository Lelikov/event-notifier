import pytest
import respx
from httpx import AsyncClient, Response

from event_notifier.domain.models.notification import ChannelContact, ChannelType
from event_notifier.infrastructure.channels.telegram import TelegramChannel


@pytest.fixture
async def telegram_channel():
    async with AsyncClient(base_url="https://api.telegram.org") as client:
        yield TelegramChannel(http_client=client, bot_token="test-token")


@pytest.fixture
def contact():
    return ChannelContact(
        channel=ChannelType.TELEGRAM,
        contact_id="987654321",
        user_id="uuid-org-001",
        role="organizer",
    )


@pytest.mark.asyncio
async def test_send_returns_success_with_message_id(telegram_channel, contact):
    with respx.mock:
        respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=Response(200, json={"ok": True, "result": {"message_id": 42}})
        )

        result = await telegram_channel.send(
            contact=contact,
            trigger_event="BOOKING_CREATED",
            template_data={"booking_id": "b-1"},
        )

    assert result.success is True
    assert result.message_id == "42"
    assert result.channel == ChannelType.TELEGRAM


@pytest.mark.asyncio
async def test_send_failure_on_forbidden(telegram_channel, contact):
    with respx.mock:
        respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"})
        )

        result = await telegram_channel.send(
            contact=contact,
            trigger_event="BOOKING_CREATED",
            template_data={},
        )

    assert result.success is False
    assert "403" in result.error
