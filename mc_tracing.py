"""
MikeCast — agentless OpenTelemetry tracing, exported directly to Datadog.

No Datadog Agent, no OTel Collector, no sidecar container: spans are batched
in-process and POSTed straight to Datadog's OTLP intake
(https://otlp.<DD_SITE>/v1/traces, http/protobuf) using DD_API_KEY as the
`dd-api-key` header. This is the pattern Datadog recommends for serverless /
ephemeral compute where running an Agent isn't feasible — see DATADOG_PLAN.md
Phase 1. When DD_API_KEY is unset, all spans are no-ops (NoOpTracer).

One root span per daily run ("mikecast.daily_run"), with one child span per
pipeline step via `step_span()`.
"""

import logging
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from mc_config import DD_API_KEY, DD_SITE

logger = logging.getLogger("mikecast")

_provider: TracerProvider | None = None


def init_tracing(service_name: str = "mikecast", env: str = "production") -> trace.Tracer:
    """Set up the global TracerProvider. Safe to call once at process start."""
    global _provider

    if not DD_API_KEY:
        logger.info("DD_API_KEY not set — tracing disabled (no-op tracer).")
        return trace.get_tracer(service_name)

    resource = Resource.create({
        "service.name": service_name,
        "deployment.environment": env,
        # Fixed, not socket.gethostname(): Fargate assigns a new container
        # hostname on every task launch, and Datadog's APM host billing can
        # key off host.name on trace data. A real hostname here would report
        # a new "host" every day; this pins it to one stable host instead.
        "host.name": "mikecast-fargate",
    })
    _provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"https://otlp.{DD_SITE}/v1/traces",
        headers={"dd-api-key": DD_API_KEY, "compute_stats": "true"},
    )
    _provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(_provider)
    logger.info("Datadog OTLP tracing initialized (site=%s).", DD_SITE)
    return trace.get_tracer(service_name)


def shutdown_tracing() -> None:
    """Flush and shut down the exporter. Call once at the very end of the run —
    this is a short-lived process, so anything unflushed at exit is lost."""
    if _provider is None:
        return
    try:
        _provider.force_flush()
        _provider.shutdown()
    except Exception as exc:
        logger.warning("Tracing shutdown/flush failed (non-fatal): %s", exc)


@contextmanager
def step_span(name: str, pipeline_path: str, **attributes):
    """Child span for one pipeline step. Records the exception and marks the
    span ERROR on failure, then re-raises — never swallows errors."""
    tracer = trace.get_tracer("mikecast")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("mikecast.pipeline_path", pipeline_path)
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
