from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RetrievedChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    score: float
    page: int | None = None
    section: str | None = None
    snippet: str | None = None
    text: str | None = None  # full text only when CONTEXT_CAPTURE_MODE=full
    doc_type: str | None = None  # NRC 도메인: collection 값 (10CFR/DSRS/FR/RG/SRP/nuscale_*)
    revision: str | None = None
    response_date: str | None = None  # ADAMS DocumentDate 또는 govinfo dateIssued
    # NRC nrc-all-v1 스키마 전용 필드 (선택, 다운스트림 인용/필터링용)
    collection: str | None = None
    search_type: str | None = None  # "manual" | "nuscale"
    source_id: str | None = None  # ADAMS Accession Number 또는 govinfo packageId
    page_end: int | None = None
    title: str | None = None  # doc_metadata.DocumentTitle 또는 doc_metadata.title


class RetrieverSearchInput(BaseModel):
    """v2 §8 — `retriever.search` 입력 스키마. 모든 adapter가 이 모델만 받는다."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    top_k: int = 3
    scenario_object: str | None = None
    scenario_depth: str | None = None
    entities: dict[str, list[str]] = Field(default_factory=dict)


class RetrieverSearchOutput(BaseModel):
    """v2 §8 — `retriever.search` 출력 스키마. ToolResult.output에 dump되어 실린다."""

    model_config = ConfigDict(frozen=True)

    chunks: list[RetrievedChunk] = Field(default_factory=list)
