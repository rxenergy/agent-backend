from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.application.context.citation_format import format_citation, infer_doc_type
from app.domain.interaction import ChatTurn
from app.domain.memory import MemoryRef
from app.domain.retrieval import RetrievedChunk

CaptureMode = Literal["metadata", "snippets", "full"]

# 인덱싱 단계에서 본문에서 분리된 표 마커. 본문의 `[TABLE: tb_0001]` 를 chunk.tables
# 배열의 매칭 엔트리(tag)로 인라인 치환한다(spec_driven_table_inline_expansion).
_TABLE_MARKER_RE = re.compile(r"\[TABLE:\s*(?P<tag>[^\]]+?)\s*\]")


def _render_table_entry(entry: dict[str, Any]) -> str | None:
    """tables 엔트리 → 치환 텍스트. caption(있으면) + markdown 을 결합한다. markdown
    이 비면(표 본문 없음) None 반환 → 호출부가 마커를 보존한다."""
    if not isinstance(entry, dict):
        return None
    table = (entry.get("markdown") or entry.get("html") or "").strip()
    if not table:
        return None
    caption = (entry.get("caption") or "").strip()
    return f"**{caption}**\n\n{table}" if caption else table


def _expand_tables(body: str, tables: list[dict[str, Any]] | None) -> str:
    """본문의 `[TABLE: tag]` 마커를 tables 배열에서 같은 `tag` 엔트리의 caption+markdown
    으로 인라인 치환한다. 같은 tag 의 엔트리가 여러 개면 배열 순서대로 **누적 결합**한다
    (분할된 표·동일 태그 다중 표). tables 가 없거나 해당 tag 가 없으면 마커를 그대로
    둔다(silent 삭제 금지 — 표 누락을 가시화, CLAUDE.md #6)."""
    if not tables:
        return body
    # tag → 엔트리 *리스트*(배열을 매번 선형탐색하지 않도록 1회 구성). dict/문자열tag 만.
    # 같은 tag 가 중복되면 배열 순서를 보존해 누적한다(덮어쓰지 않음).
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for e in tables:
        if isinstance(e, dict) and isinstance(e.get("tag"), str):
            by_tag.setdefault(e["tag"], []).append(e)
    if not by_tag:
        return body

    # 본문에 같은 tag 마커가 여러 번 나오면 표는 *한 번만* 렌더링한다(중복 삽입 방지).
    # 첫 마커에 표를 싣고, 동일 tag 의 이후 마커는 제거한다.
    seen: set[str] = set()

    def _sub(m: re.Match[str]) -> str:
        tag = m.group("tag").strip()
        entries = by_tag.get(tag) or ()
        rendered = [r for e in entries if (r := _render_table_entry(e)) is not None]
        if not rendered:
            return m.group(0)  # 미매칭(또는 표 본문 없음)=마커 보존
        if tag in seen:
            return ""  # 동일 tag 두 번째 이후 마커 — 표 중복 삽입 대신 제거
        seen.add(tag)
        # 누적 결합 — 같은 tag 의 여러 *엔트리*를 배열 순서대로 빈 줄로 잇는다.
        return "\n\n".join(rendered)

    return _TABLE_MARKER_RE.sub(_sub, body)


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
    # 원문 다운로드 URL(인덱스 doc_metadata 1차 소스) + 조문 ID — References 딥링크/
    # 라벨 구성용. answer_renderer 가 source_url 우선 → adams_url 재구성 → 평문 순으로
    # 강등하고, clause_id(10CFR50.46)로 "10 CFR §50.46" 라벨을 만든다.
    source_url: str | None = None
    clause_id: str | None = None


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
                    source_url=c.source_url,
                    clause_id=c.clause_id,
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
            # 표 마커 인라인 치환 — capture_mode 무관(있으면 치환). full 모드(text 전문)
            # 는 마커가 잘리지 않아 전량 치환되고, snippets 모드(타 variant)는 캡 안에
            # 든 마커만 치환된다(잘린 마커는 보존돼 가시화).
            body = _expand_tables(body, chunk.tables)
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
