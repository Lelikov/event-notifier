"""OpenTelemetry tracing bootstrap. Identical across services; configured via OTEL_* env.

No-op unless OTEL_SDK_DISABLED is falsy (default in the base stack is disabled; the
observability compose profile / Helm enables it and sets OTEL_EXPORTER_OTLP_ENDPOINT).
"""

import os

from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


class SentryTracePropagator(TextMapPropagator):
    """Extract a Sentry `sentry-trace` header into an OTel remote SpanContext.

    Format: ``<32-hex traceid>-<16-hex spanid>[-<sampled 0|1>]``. Inject is a
    no-op (this service emits W3C traceparent, not sentry-trace). Lets the
    frontend's Sentry trace id continue into the backend trace (and Tempo).
    """

    _FIELD = "sentry-trace"

    def extract(self, carrier: CarrierT, context: Context | None = None, getter: Getter = default_getter) -> Context:
        if context is None:
            context = Context()
        values = getter.get(carrier, self._FIELD)
        if not values:
            return context
        parts = values[0].split("-")
        if len(parts) < 2 or len(parts[0]) != 32 or len(parts[1]) != 16:
            return context
        try:
            trace_id = int(parts[0], 16)
            span_id = int(parts[1], 16)
        except ValueError:
            return context
        sampled = len(parts) > 2 and parts[2] == "1"
        flags = TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT)
        span_context = SpanContext(trace_id=trace_id, span_id=span_id, is_remote=True, trace_flags=flags)
        return set_span_in_context(NonRecordingSpan(span_context), context)

    def inject(self, carrier: CarrierT, context: Context | None = None, setter: Setter = default_setter) -> None:  # noqa: ARG002
        return

    @property
    def fields(self) -> set:
        return {self._FIELD}


def _disabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "").strip().lower() in ("true", "1", "yes")


def setup_tracing() -> None:
    """Install the global TracerProvider + W3C propagators. Idempotent; no-op when disabled."""
    if _disabled():
        return
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=Resource.create())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    set_global_textmap(
        CompositePropagator(
            [TraceContextTextMapPropagator(), W3CBaggagePropagator(), SentryTracePropagator()],
        ),
    )


def instrument_fastapi(app: object) -> None:
    """Auto-instrument the FastAPI app (server spans + traceparent extraction) and httpx clients."""
    if _disabled():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()


def instrument_asyncpg() -> None:
    """Auto-instrument asyncpg (DB query spans). Call only from services with a database."""
    if _disabled():
        return
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor  # noqa: PLC0415

    AsyncPGInstrumentor().instrument()


def rabbit_telemetry_middlewares() -> list:
    """FastStream RabbitMQ telemetry middleware(s) — span creation + traceparent over AMQP."""
    if _disabled():
        return []
    from faststream.rabbit.opentelemetry import RabbitTelemetryMiddleware  # noqa: PLC0415

    return [RabbitTelemetryMiddleware(tracer_provider=trace.get_tracer_provider())]


def add_otel_trace_context(
    _logger: object,
    _method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    """Structlog processor: stamp the active span's W3C trace/span id onto every log line."""
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict
