"""Tests for channel behavior driven by NotificationBinding entries."""

import pytest
from event_schemas.types import TriggerEvent

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.infrastructure.channels.telegram import TelegramChannel


class _Sql:
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


@pytest.mark.anyio
async def test_telegram_renders_binding_body():
    rows = [{"trigger_event": "BOOKING_CREATED", "channel": "telegram", "enabled": True,
             "unisender_template_id": None, "telegram_body": "Привет, {{ client_name }}!"}]
    bindings = BindingsProvider(sql=_Sql(rows), ttl_seconds=60)
    chan = TelegramChannel(http_client=None, bot_token="t", bindings=bindings)
    text = await chan._render(TriggerEvent.BOOKING_CREATED, {"client_name": "Анна"})
    assert text == "Привет, Анна!"


@pytest.mark.anyio
async def test_telegram_skips_when_disabled():
    rows = [{"trigger_event": "BOOKING_CREATED", "channel": "telegram", "enabled": False,
             "unisender_template_id": None, "telegram_body": "x"}]
    bindings = BindingsProvider(sql=_Sql(rows), ttl_seconds=60)
    chan = TelegramChannel(http_client=None, bot_token="t", bindings=bindings)
    assert await chan._render(TriggerEvent.BOOKING_CREATED, {}) is None
