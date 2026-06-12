"""Tests for the /health (liveness) and /ready (readiness) endpoints."""

import asyncio

from event_notifier import main


class _FakeConsumer:
    def __init__(self, *, started: bool) -> None:
        self.started = started


class _FakeRepository:
    def __init__(self, *, db_ok: bool = True, error: Exception | None = None) -> None:
        self._db_ok = db_ok
        self._error = error

    async def healthcheck(self) -> bool:
        if self._error is not None:
            raise self._error
        return self._db_ok


async def _alive_task() -> asyncio.Task:
    async def _sleep_forever() -> None:
        await asyncio.sleep(3600)

    return asyncio.create_task(_sleep_forever())


def _set_state(*, consumer: object, sender_task: object, repository: object) -> None:
    main.app.state.consumer = consumer
    main.app.state.sender_task = sender_task
    main.app.state.repository = repository


class TestHealth:
    async def test_health_is_shallow_even_when_deps_are_down(self) -> None:
        _set_state(consumer=None, sender_task=None, repository=None)

        assert await main.health() == {"status": "ok"}

    def test_routes_registered(self) -> None:
        paths = {route.path for route in main.app.routes}

        assert "/health" in paths
        assert "/ready" in paths


class TestReady:
    async def test_ready_when_all_checks_pass(self) -> None:
        sender_task = await _alive_task()
        _set_state(consumer=_FakeConsumer(started=True), sender_task=sender_task, repository=_FakeRepository())

        response = await main.ready()
        sender_task.cancel()

        assert response.status_code == 200
        assert b'"status":"ready"' in response.body

    async def test_not_ready_when_database_down(self) -> None:
        sender_task = await _alive_task()
        _set_state(
            consumer=_FakeConsumer(started=True),
            sender_task=sender_task,
            repository=_FakeRepository(error=ConnectionError("db down")),
        )

        response = await main.ready()
        sender_task.cancel()

        assert response.status_code == 503
        assert b'"status":"not_ready"' in response.body
        assert b'"database":false' in response.body

    async def test_not_ready_when_consumer_not_started(self) -> None:
        sender_task = await _alive_task()
        _set_state(consumer=_FakeConsumer(started=False), sender_task=sender_task, repository=_FakeRepository())

        response = await main.ready()
        sender_task.cancel()

        assert response.status_code == 503
        assert b'"consumer":false' in response.body
