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


def _row(trigger, role, channel, enabled, uid=None, tb=None):
    return {
        "trigger_event": trigger,
        "recipient_role": role,
        "channel": channel,
        "enabled": enabled,
        "unisender_template_id": uid,
        "telegram_body": tb,
    }


@pytest.mark.anyio
async def test_get_distinguishes_role_and_caches():
    rows = [
        _row("BOOKING_CREATED", "client", "email", True, uid="uuid-client"),
        _row("BOOKING_CREATED", "organizer", "email", True, uid="uuid-organizer"),
    ]
    sql = _FakeSql(rows)
    provider = BindingsProvider(sql=sql, ttl_seconds=60)

    client = await provider.get("BOOKING_CREATED", "client", ChannelType.EMAIL)
    organizer = await provider.get("BOOKING_CREATED", "organizer", ChannelType.EMAIL)
    assert client is not None and client.unisender_template_id == "uuid-client"
    assert organizer is not None and organizer.unisender_template_id == "uuid-organizer"
    assert client.recipient_role == "client"
    assert sql.calls == 1  # second get served from cache


@pytest.mark.anyio
async def test_missing_role_binding_returns_none():
    rows = [_row("BOOKING_CREATED", "client", "email", True)]
    provider = BindingsProvider(sql=_FakeSql(rows), ttl_seconds=60)
    assert await provider.get("BOOKING_CREATED", "organizer", ChannelType.EMAIL) is None
