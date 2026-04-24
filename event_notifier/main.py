"""FastAPI application entry point for event-notifier."""

import asyncio
from contextlib import asynccontextmanager
from logging import getLevelNamesMapping
from typing import TYPE_CHECKING

import structlog
from dishka import make_async_container
from fastapi import FastAPI

from event_notifier.adapters.consumer import NotificationConsumer
from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.ioc import AppProvider
from event_notifier.logger import setup_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    container = make_async_container(AppProvider())

    settings = await container.get(Settings)
    log_level = getLevelNamesMapping().get(settings.log_level.upper(), 20)
    setup_logger(log_level=log_level, console_render=settings.debug)

    logger.info("Starting event-notifier", log_level=settings.log_level)

    # Start RabbitMQ consumer
    consumer = await container.get(NotificationConsumer)
    await consumer.start()

    # Start OutboxSender as background asyncio task
    outbox_sender = await container.get(OutboxSender)
    sender_task = asyncio.create_task(outbox_sender.start(), name="outbox-sender")

    # Start periodic cleanup of processed_events table
    repository = await container.get(NotificationRepository)

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(3600)  # every hour
            try:
                await repository.cleanup_processed_events(days=7)
            except Exception:
                logger.exception("processed_events cleanup failed")

    cleanup_task = asyncio.create_task(_cleanup_loop(), name="processed-events-cleanup")

    logger.info("event-notifier ready")

    yield

    logger.info("Shutting down event-notifier")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    outbox_sender.stop()
    sender_task.cancel()
    try:
        await sender_task
    except asyncio.CancelledError:
        pass

    await consumer.stop()
    await container.close()


app = FastAPI(title="event-notifier", version="0.3.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
