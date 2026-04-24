import pytest
import respx
from httpx import AsyncClient, Response

from event_notifier.domain.models.notification import ChannelContact, ChannelType
from event_notifier.infrastructure.channels.email import EmailChannel


@pytest.fixture
async def email_channel():
    async with AsyncClient(base_url="https://go.unisender.ru") as client:
        yield EmailChannel(
            http_client=client,
            api_key="test-key",
            from_email="noreply@example.com",
            from_name="Test",
        )


@pytest.fixture
def contact():
    return ChannelContact(
        channel=ChannelType.EMAIL,
        contact_id="recipient@example.com",
        user_id="uuid-recipient-001",
        role="client",
    )


@pytest.mark.asyncio
async def test_send_returns_success_with_job_id(email_channel, contact):
    with respx.mock:
        respx.post("https://go.unisender.ru/ru/transactional/api/v1/email/send.json").mock(
            return_value=Response(200, json={"status": "success", "job_id": "job-xyz"})
        )

        result = await email_channel.send(
            contact=contact,
            trigger_event="BOOKING_CREATED",
            template_data={"booking_id": "b-1"},
        )

    assert result.success is True
    assert result.message_id == "job-xyz"
    assert result.channel == ChannelType.EMAIL


@pytest.mark.asyncio
async def test_send_returns_failure_on_api_error(email_channel, contact):
    with respx.mock:
        respx.post("https://go.unisender.ru/ru/transactional/api/v1/email/send.json").mock(
            return_value=Response(400, json={"status": "error", "message": "Invalid API key"})
        )

        result = await email_channel.send(
            contact=contact,
            trigger_event="BOOKING_CREATED",
            template_data={},
        )

    assert result.success is False
    assert result.error is not None
