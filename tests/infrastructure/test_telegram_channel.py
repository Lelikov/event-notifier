"""Tests for TelegramChannel: Jinja2 rendering and error classification."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, Response
from jinja2 import Environment, FileSystemLoader

from event_notifier.domain.models.notification import ChannelContact, ChannelType
from event_notifier.infrastructure.channels.telegram import TelegramChannel

SEND_URL = "https://api.telegram.org/bottest-token/sendMessage"

_TEMPLATES_DIR = Path(__file__).parents[2] / "event_notifier" / "templates"


@pytest.fixture
def template_env():
    return Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True)


@pytest.fixture
async def telegram_channel(template_env):
    async with AsyncClient(base_url="https://api.telegram.org") as client:
        yield TelegramChannel(http_client=client, bot_token="test-token", template_env=template_env)


@pytest.fixture
def contact():
    return ChannelContact(
        channel=ChannelType.TELEGRAM,
        contact_id="chat-123",
        user_id="uuid-1",
        email="cli@example.com",
        role="client",
    )


async def test_renders_booking_details_from_template_data(telegram_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 42}}))

        result = await telegram_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CANCELLED,
            template_data={"start_time": "2026-06-12 10:00", "cancellation_reason": "болезнь"},
        )

    assert result.success is True
    assert result.message_id == "42"
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == "chat-123"
    assert "Встреча отменена" in body["text"]
    assert "2026-06-12 10:00" in body["text"]
    assert "болезнь" in body["text"]
    # The internal trigger name must never leak to the user
    assert "BOOKING_CANCELLED" not in body["text"]


@pytest.mark.parametrize("trigger", list(TriggerEvent))
@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_every_trigger_has_a_template_in_every_locale(telegram_channel, contact, trigger, locale):
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 1}}))

        result = await telegram_channel.send(contact=contact, trigger_event=trigger, template_data={"locale": locale})

    assert result.success is True


@pytest.mark.parametrize(
    ("locale", "expected_fragment"),
    [("ru", "обратитесь к организатору"), ("en", "contact the organizer")],
)
async def test_blacklisted_rejection_template_is_neutral(telegram_channel, contact, locale, expected_fragment):
    """Dedicated blacklist-rejection template per locale; the wording never mentions the blacklist."""
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 7}}))

        result = await telegram_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_REJECTED_BLACKLISTED,
            template_data={"locale": locale},
        )

    assert result.success is True
    text = json.loads(route.calls[0].request.content)["text"]
    assert expected_fragment in text
    lowered = text.lower()
    assert "blacklist" not in lowered
    assert "черный список" not in lowered
    assert "чёрный список" not in lowered
    assert "BOOKING_REJECTED_BLACKLISTED" not in text


async def test_en_locale_selects_english_template(telegram_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 1}}))

        result = await telegram_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CANCELLED,
            template_data={"locale": "en", "start_time": "2026-06-12 10:00", "cancellation_reason": "illness"},
        )

    assert result.success is True
    body = json.loads(route.calls[0].request.content)
    assert "Meeting cancelled" in body["text"]
    assert "illness" in body["text"]


async def test_missing_locale_falls_back_to_default_russian(telegram_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 1}}))

        result = await telegram_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CANCELLED,
            template_data={"start_time": "2026-06-12 10:00"},
        )

    assert result.success is True
    body = json.loads(route.calls[0].request.content)
    assert "Встреча отменена" in body["text"]


async def test_unknown_locale_falls_back_to_default_russian(telegram_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 1}}))

        result = await telegram_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CANCELLED,
            template_data={"locale": "fr"},
        )

    assert result.success is True
    body = json.loads(route.calls[0].request.content)
    assert "Встреча отменена" in body["text"]


async def test_regional_locale_variant_maps_to_primary_language(telegram_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 1}}))

        result = await telegram_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CANCELLED,
            template_data={"locale": "en-GB"},
        )

    assert result.success is True
    body = json.loads(route.calls[0].request.content)
    assert "Meeting cancelled" in body["text"]


async def test_unknown_trigger_fails_permanently(template_env, contact):
    env = Environment(loader=FileSystemLoader("/nonexistent"), autoescape=True)
    async with AsyncClient(base_url="https://api.telegram.org") as client:
        channel = TelegramChannel(http_client=client, bot_token="test-token", template_env=env)
        result = await channel.send(contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={})

    assert result.success is False
    assert result.retryable is False
    assert "No telegram template" in result.error


async def test_html_in_template_data_is_escaped(telegram_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 1}}))

        await telegram_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CANCELLED,
            template_data={"cancellation_reason": "<script>alert(1)</script>"},
        )

    body = json.loads(route.calls[0].request.content)
    assert "<script>" not in body["text"]


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
async def test_transient_statuses_are_retryable(telegram_channel, contact, status_code):
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=Response(status_code))

        result = await telegram_channel.send(
            contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={}
        )

    assert result.success is False
    assert result.retryable is True


async def test_4xx_is_permanent(telegram_channel, contact):
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=Response(400, json={"description": "chat not found"}))

        result = await telegram_channel.send(
            contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={}
        )

    assert result.success is False
    assert result.retryable is False


async def test_transport_error_is_retryable(telegram_channel, contact):
    with respx.mock:
        respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("refused"))

        result = await telegram_channel.send(
            contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={}
        )

    assert result.success is False
    assert result.retryable is True
