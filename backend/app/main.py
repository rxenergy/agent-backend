from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, openai_compat
from app.config.profiles import build_container, shutdown_container
from app.config.settings import get_settings
from app.observability.logging import configure_logging, get_logger
from app.observability.otel import install_tracing, instrument_fastapi


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    install_tracing(
        service_name=settings.otel_service_name,
        service_version=settings.service_version,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        enabled=settings.otel_enabled,
    )
    container = await build_container(settings)
    app.state.container = container
    get_logger("startup").info(
        "agent_backend_started",
        app_profile=settings.app_profile,
        agent_variant=settings.agent_variant,
        event_sink=settings.event_sink,
        memory_store=settings.memory_store,
        otel_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    try:
        yield
    finally:
        await shutdown_container(container)


def create_app() -> FastAPI:
    app = FastAPI(
        title="SMR Agent Backend",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(openai_compat.router)
    instrument_fastapi(app)
    return app


app = create_app()
