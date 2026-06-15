"""FastAPI application entry point for event-notifier."""

import asyncio
from contextlib import asynccontextmanager
from logging import getLevelNamesMapping
from typing import TYPE_CHECKING

import structlog
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from event_notifier import metrics
from event_notifier.adapters.consumer import NotificationConsumer
from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.ioc import AppProvider
from event_notifier.logger import setup_logger
from event_notifier.routes_admin import router as admin_router
from event_notifier.telemetry import instrument_asyncpg, instrument_fastapi, setup_tracing

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = structlog.get_logger(__name__)

# Container created at module level so setup_dishka can use it before lifespan.
# It is started lazily (providers resolve on first get()) and closed in lifespan shutdown.
_container = make_async_container(AppProvider(), FastapiProvider())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    container = _container

    settings = await container.get(Settings)
    log_level = getLevelNamesMapping().get(settings.log_level.upper(), 20)
    setup_logger(log_level=log_level, console_render=settings.debug)

    logger.info("Starting event-notifier", log_level=settings.log_level)

    consumer = await container.get(NotificationConsumer)
    await consumer.start()

    outbox_sender = await container.get(OutboxSender)
    sender_task = asyncio.create_task(outbox_sender.start(), name="outbox-sender")

    repository = await container.get(NotificationRepository)

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(3600)  # every hour
            try:
                await repository.cleanup_processed_events(days=7)
            except Exception:
                logger.exception("processed_events cleanup failed")

    cleanup_task = asyncio.create_task(_cleanup_loop(), name="processed-events-cleanup")

    app.state.consumer = consumer
    app.state.sender_task = sender_task
    app.state.repository = repository

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


app = FastAPI(title="event-notifier", version="0.4.0", lifespan=lifespan)
setup_tracing()
instrument_fastapi(app)
instrument_asyncpg()
setup_dishka(container=_container, app=app)
app.include_router(admin_router)


async def _collect_health_checks(application: FastAPI) -> dict[str, bool]:
    consumer = getattr(application.state, "consumer", None)
    sender_task = getattr(application.state, "sender_task", None)
    repository = getattr(application.state, "repository", None)

    checks = {
        "consumer": consumer is not None and consumer.started,
        "outbox_sender": sender_task is not None and not sender_task.done(),
        "database": False,
    }
    if repository is not None:
        try:
            checks["database"] = await repository.healthcheck()
        except Exception:
            logger.exception("Health check: database unreachable")
    return checks


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe: the process is up and serving HTTP. No dependency calls."""
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus exposition endpoint (consumer RED, delivery counters, outbox gauges)."""
    return metrics.metrics_response()


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe: consumer started, outbox sender task alive, DB reachable."""
    checks = await _collect_health_checks(app)
    ready_ok = all(checks.values())
    status_code = 200 if ready_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if ready_ok else "not_ready", "checks": checks},
    )
