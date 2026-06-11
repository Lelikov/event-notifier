"""Container smoke test: every provider on the startup path must resolve.

Regression for the httpx.Timeout(connect/read/write without pool) ValueError
that crashed DI resolution at boot and was never executed by any test.
"""

import pytest
from dishka import make_async_container

from event_notifier.adapters.consumer import NotificationConsumer
from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.adapters.result_publisher import DeliveryResultPublisher
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.ioc import AppProvider

ENV = {
    "DATABASE_URL": "postgresql+asyncpg://postgres:password@localhost:5432/event_notifier",
    "EVENT_USERS_URL": "http://localhost:8001",
    "EVENT_USERS_TOKEN": "token",
    "EVENTS_ENDPOINT_URL": "http://localhost:8000/event/admin",
    "EVENTS_API_KEY": "key",
    "UNISENDER_API_KEY": "key",
    "UNISENDER_FROM_EMAIL": "noreply@example.com",
    "UNISENDER_TEMPLATE_IDS": '{"BOOKING_CREATED": "tmpl-1"}',
    "TELEGRAM_BOT_TOKEN": "token",
}


@pytest.fixture
def env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


async def test_container_resolves_full_startup_graph(env):
    container = make_async_container(AppProvider())
    try:
        settings = await container.get(Settings)
        assert settings.unisender_template_ids == {"BOOKING_CREATED": "tmpl-1"}

        consumer = await container.get(NotificationConsumer)
        assert consumer is not None

        sender = await container.get(OutboxSender)
        assert sender is not None

        repository = await container.get(NotificationRepository)
        assert repository is not None

        publisher = await container.get(DeliveryResultPublisher)
        assert publisher is not None
    finally:
        await container.close()


async def test_container_resolves_without_optional_result_endpoint(env, monkeypatch):
    monkeypatch.delenv("EVENTS_ENDPOINT_URL")
    container = make_async_container(AppProvider())
    try:
        publisher = await container.get(DeliveryResultPublisher)
        assert publisher is not None
    finally:
        await container.close()
