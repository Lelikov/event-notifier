"""Tests for channel behavior driven by NotificationBinding entries (role-aware)."""

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


def _row(role, body, enabled=True):
    return {
        "trigger_event": "BOOKING_CREATED",
        "recipient_role": role,
        "channel": "telegram",
        "enabled": enabled,
        "unisender_template_id": None,
        "telegram_body": body,
    }


@pytest.mark.anyio
async def test_telegram_renders_role_specific_body():
    rows = [
        _row("client", "Клиент {{ client_name }}"),
        _row("organizer", "Волонтёр {{ client_name }}"),
    ]
    bindings = BindingsProvider(sql=_Sql(rows), ttl_seconds=60)
    chan = TelegramChannel(http_client=None, bot_token="t", bindings=bindings)
    client_text = await chan._render(TriggerEvent.BOOKING_CREATED, "client", {"client_name": "Анна"})
    organizer_text = await chan._render(TriggerEvent.BOOKING_CREATED, "organizer", {"client_name": "Анна"})
    assert client_text == "Клиент Анна"
    assert organizer_text == "Волонтёр Анна"


@pytest.mark.anyio
async def test_telegram_skips_when_role_binding_disabled():
    rows = [_row("client", "x", enabled=False)]
    bindings = BindingsProvider(sql=_Sql(rows), ttl_seconds=60)
    chan = TelegramChannel(http_client=None, bot_token="t", bindings=bindings)
    assert await chan._render(TriggerEvent.BOOKING_CREATED, "client", {}) is None
