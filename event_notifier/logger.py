import logging

import structlog

from event_notifier.telemetry import add_otel_trace_context


def setup_logger(*, log_level: int, console_render: bool) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_otel_trace_context,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer() if console_render else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=log_level)
