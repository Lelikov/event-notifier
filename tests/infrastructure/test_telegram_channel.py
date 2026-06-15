"""Tests for TelegramChannel: binding-based rendering and error classification."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, Response

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.domain.models.notification import ChannelContact, ChannelType
from event_notifier.infrastructure.channels.telegram import TelegramChannel

SEND_URL = "https://api.telegram.org/bottest-token/sendMessage"

_TEMPLATES_DIR = Path(__file__).parents[2] / "event_notifier" / "templates"


class _FakeSql:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_all(self, query, values):
        return self.rows

    async def fetch_one(self, query, values):
        return None

    async def execute(self, query, values):
        pass

    def transaction(self):
        raise NotImplementedError


def _bindings(rows) -> BindingsProvider:
    return BindingsProvider(sql=_FakeSql(rows), ttl_seconds=60)


def _tg_row(trigger: str, body: str | None, enabled: bool = True) -> dict:
    return {
        "trigger_event": trigger,
        "channel": "telegram",
        "enabled": enabled,
        "unisender_template_id": None,
        "telegram_body": body,
    }


def _load_template(locale: str, trigger: str) -> str:
    return (_TEMPLATES_DIR / locale / "telegram" / f"{trigger}.j2").read_text(encoding="utf-8")


def _all_triggers_bindings(locale: str = "ru") -> list[dict]:
    return [
        _tg_row(t.value, _load_template(locale, t.value))
        for t in TriggerEvent
    ]


@pytest.fixture
async def telegram_channel():
    bindings = _bindings(_all_triggers_bindings("ru"))
    async with AsyncClient(base_url="https://api.telegram.org") as client:
        yield TelegramChannel(http_client=client, bot_token="test-token", bindings=bindings)


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
async def test_every_trigger_has_a_ru_template(contact, trigger):
    bindings = _bindings(_all_triggers_bindings("ru"))
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=Response(200, json={"result": {"message_id": 1}}))
        async with AsyncClient(base_url="https://api.telegram.org") as client:
            channel = TelegramChannel(http_client=client, bot_token="test-token", bindings=bindings)
            result = await channel.send(contact=contact, trigger_event=trigger, template_data={})

    assert result.success is True


async def test_unknown_trigger_fails_permanently(contact):
    bindings = _bindings([])  # no bindings at all
    async with AsyncClient(base_url="https://api.telegram.org") as client:
        channel = TelegramChannel(http_client=client, bot_token="test-token", bindings=bindings)
        result = await channel.send(contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={})

    assert result.success is False
    assert result.retryable is False
    assert "No telegram template" in result.error


async def test_disabled_binding_fails_permanently(contact):
    bindings = _bindings([_tg_row("BOOKING_CREATED", "Hello!", enabled=False)])
    async with AsyncClient(base_url="https://api.telegram.org") as client:
        channel = TelegramChannel(http_client=client, bot_token="test-token", bindings=bindings)
        result = await channel.send(contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={})

    assert result.success is False
    assert result.retryable is False


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
