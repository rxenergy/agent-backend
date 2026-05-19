from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_INSTALLED = False


def install_tracing(
    *,
    service_name: str,
    service_version: str,
    otlp_endpoint: str,
    enabled: bool,
) -> None:
    global _INSTALLED
    if not enabled or _INSTALLED:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _INSTALLED = True


def instrument_fastapi(app) -> None:  # noqa: ANN001 (fastapi.FastAPI lazy-imported)
    FastAPIInstrumentor.instrument_app(app)


def get_tracer(name: str = "agent"):  # noqa: ANN201
    return trace.get_tracer(name)
