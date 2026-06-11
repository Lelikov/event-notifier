"""Tests for NotificationRepository SQL behavior via a fake executor."""

from contextlib import asynccontextmanager

from event_notifier.db.repository import NotificationRepository


class FakeTx:
    def __init__(self, claim_result):
        self.claim_result = claim_result
        self.statements: list[tuple[str, dict]] = []

    async def fetch_one(self, query: str, values: dict):
        self.statements.append((query, values))
        return self.claim_result

    async def fetch_all(self, query: str, values: dict):
        self.statements.append((query, values))
        return []

    async def execute(self, query: str, values: dict) -> None:
        self.statements.append((query, values))


class FakeSqlExecutor:
    def __init__(self, *, claim_result=None, fetch_all_result=None, fetch_one_result=None):
        self.tx = FakeTx(claim_result)
        self.fetch_all_result = fetch_all_result or []
        self.fetch_one_result = fetch_one_result
        self.statements: list[tuple[str, dict]] = []

    async def fetch_one(self, query: str, values: dict):
        self.statements.append((query, values))
        return self.fetch_one_result

    async def fetch_all(self, query: str, values: dict):
        self.statements.append((query, values))
        return self.fetch_all_result

    async def execute(self, query: str, values: dict) -> None:
        self.statements.append((query, values))

    @asynccontextmanager
    async def transaction(self):
        yield self.tx


def make_outbox_dict() -> dict:
    return {
        "idempotency_key": "evt-1:a@b.c:email",
        "cloud_event_id": "evt-1",
        "booking_id": "b-1",
        "user_id": "u-1",
        "recipient_email": "a@b.c",
        "recipient_address": "a@b.c",
        "recipient_role": "client",
        "channel": "email",
        "trigger_event": "BOOKING_CREATED",
        "template_context": {"k": "v"},
    }


async def test_write_outbox_claims_event_first_and_inserts_in_same_tx():
    sql = FakeSqlExecutor(claim_result={"cloud_event_id": "evt-1"})
    repo = NotificationRepository(sql=sql)

    written = await repo.write_outbox_atomically("evt-1", [make_outbox_dict()])

    assert written is True
    assert "INSERT INTO processed_events" in sql.tx.statements[0][0]
    assert "RETURNING" in sql.tx.statements[0][0]
    assert "INSERT INTO notification_outbox" in sql.tx.statements[1][0]
    assert sql.tx.statements[1][1]["trigger_event"] == "BOOKING_CREATED"
    assert sql.tx.statements[1][1]["recipient_email"] == "a@b.c"


async def test_write_outbox_early_exits_when_event_already_claimed():
    sql = FakeSqlExecutor(claim_result=None)
    repo = NotificationRepository(sql=sql)

    written = await repo.write_outbox_atomically("evt-1", [make_outbox_dict()])

    assert written is False
    assert len(sql.tx.statements) == 1  # only the claim attempt, no outbox inserts


async def test_fetch_pending_outbox_claims_with_skip_locked():
    sql = FakeSqlExecutor(fetch_all_result=[])
    repo = NotificationRepository(sql=sql)

    await repo.fetch_pending_outbox(batch_size=5)

    query = sql.statements[0][0]
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "status = 'processing'" in query
    assert "trigger_event" in query


async def test_reaper_returns_stale_processing_to_pending():
    sql = FakeSqlExecutor(fetch_all_result=[{"id": "r1"}, {"id": "r2"}])
    repo = NotificationRepository(sql=sql)

    count = await repo.reap_stale_processing(stale_after_seconds=300)

    assert count == 2
    query = sql.statements[0][0]
    assert "status = 'pending'" in query
    assert "retry_count = retry_count + 1" in query
    assert "status = 'processing'" in query


async def test_mark_failed_stores_last_error():
    sql = FakeSqlExecutor()
    repo = NotificationRepository(sql=sql)

    await repo.mark_failed("r1", error="boom")

    query, values = sql.statements[0]
    assert "last_error" in query
    assert values["error"] == "boom"


def test_no_bind_param_is_immediately_followed_by_pg_cast():
    # Regression: sqlalchemy text() does NOT recognize ":param::type" as a
    # bind parameter (the lookahead requires a non-colon after the name), so
    # ":template_context::jsonb" reached postgres literally and every outbox
    # insert failed with a syntax error. Use CAST(:param AS TYPE) instead.
    import inspect
    import re

    import event_notifier.db.repository as repository

    source = inspect.getsource(repository)
    offenders = re.findall(r":\w+::\w+", source)
    assert not offenders, f"bind params followed by :: cast break sqlalchemy text(): {offenders}"
