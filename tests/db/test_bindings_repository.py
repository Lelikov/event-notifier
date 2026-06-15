import pytest

from event_notifier.db.repository import NotificationRepository


class _RecordingSql:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed: list[tuple[str, dict]] = []

    async def fetch_all(self, query, values):
        self.executed.append((query, values))
        return self.rows

    async def fetch_one(self, query, values):
        return None

    async def execute(self, query, values):
        self.executed.append((query, values))


@pytest.mark.anyio
async def test_list_bindings_selects_recipient_role():
    sql = _RecordingSql(rows=[])
    repo = NotificationRepository(sql)
    await repo.list_bindings()
    query, _ = sql.executed[0]
    assert "recipient_role" in query


@pytest.mark.anyio
async def test_upsert_binding_uses_three_column_conflict():
    sql = _RecordingSql()
    repo = NotificationRepository(sql)
    await repo.upsert_binding(
        trigger_event="BOOKING_CREATED",
        recipient_role="organizer",
        channel="email",
        enabled=True,
        unisender_template_id="uuid-x",
        telegram_body=None,
    )
    query, values = sql.executed[0]
    assert "ON CONFLICT (trigger_event, recipient_role, channel)" in query
    assert values["rr"] == "organizer"
