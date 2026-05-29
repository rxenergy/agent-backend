from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.application.context.citation_format import format_citation, infer_doc_type
from app.domain.interaction import ChatTurn
from app.domain.memory import MemoryRef
from app.domain.retrieval import RetrievedChunk

CaptureMode = Literal["metadata", "snippets", "full"]


@dataclass(frozen=True)
class CitationCandidate:
    citation_id: str
    chunk_id: str
    document_id: str
    page: int | None
    score: float
    doc_type: str = "vendor"
    section: str | None = None
    revision: str | None = None
    response_date: str | None = None
    formatted: str | None = None


@dataclass(frozen=True)
class ContextPack:
    """v2 §10."""

    interaction_id: str
    query_text: str
    chat_history: tuple[ChatTurn, ...]
    conversation_summary: str | None
    scenario_object: str | None
    scenario_depth: str | None
    entities: dict[str, list[str]]
    chunks: tuple[RetrievedChunk, ...]
    citation_candidates: tuple[CitationCandidate, ...]
    memory_refs: tuple[MemoryRef, ...]
    tool_result_refs: tuple[str, ...]
    capture_mode: CaptureMode
    context_hash: str


def _hash_context(
    *,
    query_text: str,
    scenario_object: str | None,
    scenario_depth: str | None,
    chunk_ids: list[str],
    memory_ids: list[str],
) -> str:
    payload = "\n".join(
        [
            f"q={hashlib.sha256(query_text.encode('utf-8')).hexdigest()}",
            f"so={scenario_object or ''}",
            f"sd={scenario_depth or ''}",
            "chunks=" + ",".join(sorted(chunk_ids)),
            "memories=" + ",".join(sorted(memory_ids)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ContextBuilder:
    def __init__(self, capture_mode: CaptureMode = "metadata") -> None:
        self._capture_mode = capture_mode

    def build(
        self,
        *,
        interaction_id: str,
        query_text: str,
        chat_history: tuple[ChatTurn, ...],
        conversation_summary: str | None,
        scenario_object: str | None,
        scenario_depth: str | None,
        entities: dict[str, list[str]],
        chunks: list[RetrievedChunk],
        memory_refs: tuple[MemoryRef, ...] = (),
        tool_result_refs: tuple[str, ...] = (),
    ) -> ContextPack:
        candidates_list: list[CitationCandidate] = []
        for i, c in enumerate(chunks):
            cid = f"cite-{i}"
            dt = c.doc_type or infer_doc_type(c.document_id)
            candidates_list.append(
                CitationCandidate(
                    citation_id=cid,
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    page=c.page,
                    score=c.score,
                    doc_type=dt,
                    section=c.section,
                    revision=c.revision,
                    response_date=c.response_date,
                    formatted=format_citation(c, cid),
                )
            )
        candidates = tuple(candidates_list)
        context_hash = _hash_context(
            query_text=query_text,
            scenario_object=scenario_object,
            scenario_depth=scenario_depth,
            chunk_ids=[c.chunk_id for c in chunks],
            memory_ids=[m.memory_id for m in memory_refs],
        )
        return ContextPack(
            interaction_id=interaction_id,
            query_text=query_text,
            chat_history=chat_history,
            conversation_summary=conversation_summary,
            scenario_object=scenario_object,
            scenario_depth=scenario_depth,
            entities=entities,
            chunks=tuple(chunks),
            citation_candidates=candidates,
            memory_refs=memory_refs,
            tool_result_refs=tool_result_refs,
            capture_mode=self._capture_mode,
            context_hash=context_hash,
        )

    def render_for_prompt(self, pack: ContextPack) -> str:
        sections: list[str] = []
        if pack.conversation_summary:
            sections.append(f"# CONVERSATION_SUMMARY\n{pack.conversation_summary}")
        lines: list[str] = []
        for cand, chunk in zip(pack.citation_candidates, pack.chunks, strict=True):
            head = cand.formatted or (
                f"[{cand.citation_id}] {chunk.document_id}#{chunk.chunk_id} (p={chunk.page})"
            )
            if self._capture_mode == "full" and chunk.text:
                body = chunk.text
            elif self._capture_mode in ("snippets", "full") and chunk.snippet:
                body = chunk.snippet
            else:
                body = "(metadata-only capture)"
            lines.append(f"{head}\n{body}")
        sections.append("\n\n".join(lines) if lines else "(no retrieved context)")
        return "\n\n".join(sections)

    def to_snapshot(self, pack: ContextPack) -> dict[str, Any]:
        record = asdict(pack)
        # `chunks` 는 pydantic RetrievedChunk 라 asdict 가 dict 로 바꾸지 않는다
        # (deepcopy 된 객체로 남음). 명시적으로 model_dump 해 dict 화 — 안 그러면
        # metadata/snippets 모드의 필드 blanking 이 객체 subscript 로 터지고,
        # full 모드에선 sink 의 json.dumps(default=str)가 chunk 를 repr 로 직렬화한다.
        record["chunks"] = [c.model_dump(mode="json") for c in pack.chunks]
        if self._capture_mode == "metadata":
            for c in record["chunks"]:
                c["text"] = None
                c["snippet"] = None
        elif self._capture_mode == "snippets":
            for c in record["chunks"]:
                c["text"] = None
        return record
