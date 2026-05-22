from typing import Any

from pydantic import BaseModel, Field


class TranslateRequest(BaseModel):
    q: str = Field(..., description="Natural-language query")
    top_k: int = Field(10, ge=1, le=1000)
    k_dense: int = Field(50, ge=1, le=10000)
    sparse_top_n: int | None = Field(
        None, ge=1, le=5000,
        description="Override server-side sparse_top_n",
    )


class TranslateStats(BaseModel):
    dense_dim: int
    sparse_terms: int
    encode_ms: float


class TranslateResponse(BaseModel):
    dsl: dict[str, Any]
    stats: TranslateStats


class SearchRequest(TranslateRequest):
    pass


class SearchResponse(BaseModel):
    dsl: dict[str, Any]
    hits: list[dict[str, Any]]
    total: int
    took_ms: int
    encode_ms: float


class HealthResponse(BaseModel):
    e5: str
    fermi: str
    opensearch: str
    index: str
