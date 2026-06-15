import pytest

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.domain.models.notification import ChannelType


class _FakeSql:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def fetch_all(self, query, values):
        self.calls += 1
        return self.rows

    async def fetch_one(self, query, values):
        return None

    async def execute(self, query, values):
        pass

    def transaction(self):
        raise NotImplementedError


@pytest.mark.anyio
async def test_get_returns_binding_and_caches():
    rows = [
        {"trigger_event": "BOOKING_CREATED", "channel": "email", "enabled": True,
         "unisender_template_id": "uuid-1", "telegram_body": None},
        {"trigger_event": "BOOKING_CREATED", "channel": "telegram", "enabled": False,
         "unisender_template_id": None, "telegram_body": "hi {{ name }}"},
    ]
    sql = _FakeSql(rows)
    provider = BindingsProvider(sql=sql, ttl_seconds=60)

    b = await provider.get("BOOKING_CREATED", ChannelType.EMAIL)
    assert b is not None and b.enabled and b.unisender_template_id == "uuid-1"
    tg = await provider.get("BOOKING_CREATED", ChannelType.TELEGRAM)
    assert tg is not None and tg.enabled is False
    assert sql.calls == 1  # second get served from cache


@pytest.mark.anyio
async def test_missing_binding_returns_none():
    provider = BindingsProvider(sql=_FakeSql([]), ttl_seconds=60)
    assert await provider.get("BOOKING_CREATED", ChannelType.EMAIL) is None
