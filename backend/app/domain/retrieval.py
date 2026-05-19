from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    score: float
    page: int | None = None
    section: str | None = None
    snippet: str | None = None
    text: str | None = None  # full text only when CONTEXT_CAPTURE_MODE=full
    doc_type: str | None = None  # vendor | regulation | rai (기획 doc §Citation Format)
    revision: str | None = None
    response_date: str | None = None  # RAI 응답 일자
