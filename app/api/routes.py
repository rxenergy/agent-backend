from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.schemas import (
    HealthResponse,
    SearchRequest,
    SearchResponse,
    TranslateRequest,
    TranslateResponse,
    TranslateStats,
)
from app.search.client import cluster_status, execute_search
from app.search.translator import HybridQueryTranslator

logger = logging.getLogger(__name__)
router = APIRouter()


def _translator(request: Request) -> HybridQueryTranslator:
    return request.app.state.translator


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    state = request.app.state
    return HealthResponse(
        e5="ready" if state.e5_ready else "loading",
        fermi="ready" if state.fermi_ready else "loading",
        opensearch=cluster_status(state.os_client),
        index=state.settings.index_name,
    )


@router.post("/search/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest, request: Request) -> TranslateResponse:
    translator = _translator(request)
    settings = request.app.state.settings

    if req.sparse_top_n is not None:
        original_top_n = translator.fermi.top_n
        translator.fermi.top_n = req.sparse_top_n
        try:
            result = translator.translate(req.q, top_k=req.top_k, k_dense=req.k_dense)
        finally:
            translator.fermi.top_n = original_top_n
    else:
        result = translator.translate(req.q, top_k=req.top_k, k_dense=req.k_dense)

    return TranslateResponse(
        dsl=result.dsl,
        stats=TranslateStats(
            dense_dim=result.dense_dim,
            sparse_terms=result.sparse_terms,
            encode_ms=round(result.encode_ms, 3),
        ),
    )


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, request: Request) -> SearchResponse:
    state = request.app.state
    translator = _translator(request)

    result = translator.translate(req.q, top_k=req.top_k, k_dense=req.k_dense)

    try:
        os_response = execute_search(
            state.os_client,
            state.settings.index_name,
            result.dsl,
            pipeline=state.settings.search_pipeline,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("OpenSearch search failed")
        raise HTTPException(status_code=502, detail=f"OpenSearch error: {exc}") from exc

    hits_block = os_response.get("hits", {})
    total = hits_block.get("total", {})
    total_value = total.get("value", 0) if isinstance(total, dict) else int(total or 0)

    return SearchResponse(
        dsl=result.dsl,
        hits=hits_block.get("hits", []),
        total=total_value,
        took_ms=int(os_response.get("took", 0)),
        encode_ms=round(result.encode_ms, 3),
    )
