from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import settings
from app.embeddings.e5 import E5Encoder
from app.embeddings.fermi import FermiEncoder
from app.search.client import build_client
from app.search.translator import HybridQueryTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = settings
    app.state.e5_ready = False
    app.state.fermi_ready = False

    logger.info("Loading E5 (%s) on %s", settings.e5_model_id, settings.device)
    e5 = E5Encoder(
        model_id=settings.e5_model_id,
        device=settings.device,
        max_seq_len=settings.e5_max_seq_len,
    )
    e5.warmup()
    app.state.e5 = e5
    app.state.e5_ready = True

    logger.info("Loading Fermi (%s) on %s", settings.fermi_model_id, settings.device)
    fermi = FermiEncoder(
        model_id=settings.fermi_model_id,
        device=settings.device,
        max_seq_len=settings.fermi_max_seq_len,
        top_n=settings.sparse_top_n,
        weight_threshold=settings.sparse_weight_threshold,
    )
    fermi.warmup()
    app.state.fermi = fermi
    app.state.fermi_ready = True

    app.state.translator = HybridQueryTranslator(
        e5=e5,
        fermi=fermi,
        dense_field=settings.dense_field,
        sparse_field=settings.sparse_field,
    )

    app.state.os_client = build_client(settings.opensearch_url)
    logger.info("Startup complete.")

    try:
        yield
    finally:
        logger.info("Shutting down.")


app = FastAPI(
    title="agent-backend NL→OpenSearch translator",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)
