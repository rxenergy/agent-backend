from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.application.context.citation_format import (
    format_citation,
    format_table_citation,
    infer_doc_type,
)
from app.application.context.table_render import table_body_markdown
from app.domain.interaction import ChatTurn
from app.domain.memory import MemoryRef
from app.domain.retrieval import RetrievedChunk

CaptureMode = Literal["metadata", "snippets", "full"]

# 인덱싱 단계에서 본문에서 분리된 표 마커. 본문의 `[TABLE: tb_0001]` 를 chunk.tables
# 배열의 매칭 엔트리(tag)로 인라인 치환한다(spec_driven_table_inline_expansion).
_TABLE_MARKER_RE = re.compile(r"\[TABLE:\s*(?P<tag>[^\]]+?)\s*\]")


def _render_table_entry(entry: dict[str, Any]) -> str | None:
    """tables 엔트리 → 치환 텍스트. caption(있으면) + 표 본문(GFM markdown)을 결합한다.
    표 본문은 table_body_markdown 이 markdown 우선·html 은 파이프표로 변환해 돌려준다
    (raw HTML 텍스트 노출 방지). 본문이 비면 None → 호출부가 마커를 보존."""
    if not isinstance(entry, dict):
        return None
    table = table_body_markdown(entry).strip()
    if not table:
        return None
    caption = (entry.get("caption") or "").strip()
    return f"**{caption}**\n\n{table}" if caption else table


def _referenced_table_tags(body: str | None) -> set[str]:
    """본문에서 `[TABLE: tag]` 마커가 실제로 가리키는 tag 집합. 본문이 참조하지 않는
    표(인덱싱 시 같은 chunk 에 묶였으나 마커 없음)는 cite 로 승격하지 않기 위함
    (spec_driven_table_citation_granularity — 본문 미참조 표 제외). 대소문자/공백은
    마커 정규식이 흡수한다."""
    if not body:
        return set()
    return {m.group("tag").strip() for m in _TABLE_MARKER_RE.finditer(body)}


def _strip_table_markers(body: str) -> str:
    """본문에서 `[TABLE: tag]` 마커를 제거(spec_driven_table_citation_granularity D1).
    표는 # TABLES 의 독립 cite 로 분리되므로 본문에는 마커를 남기지 않는다 — 모델이
    본문 근거와 표 근거를 섞지 않게. 마커 자리에 연속 공백이 생기지 않도록 양옆 공백을
    1칸으로 정규화한다(인접 토큰 붙음·이중 공백 회피)."""
    if "[TABLE:" not in body:
        return body
    stripped = _TABLE_MARKER_RE.sub(" ", body)
    # 마커 제거로 생긴 다중 공백을 1칸으로(줄바꿈은 보존 — 단락 구조 유지).
    return re.sub(r"[ \t]{2,}", " ", stripped)


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
    # 인용 입도(spec_driven_table_citation_granularity) — chunk 본문과 개별 표를 별도
    # cite 후보로 분리해 generation 이 표 근거/본문 근거를 구분 인용하게 한다.
    #   kind="chunk" : chunk 본문 후보. tables=None(표는 별도 table 후보로 승격됨).
    #   kind="table" : 표 1개 후보. parent_chunk_id/table_tag 로 소속 표 지정,
    #                  tables=[그 표 dict] (References 가 이 단일 표를 렌더).
    # 출처 메타(document_id/page/source_url/clause_id/section/doc_type)는 table 후보도
    # parent chunk 에서 승계한다(표는 그 chunk 의 일부 — D3).
    kind: Literal["chunk", "table"] = "chunk"
    parent_chunk_id: str | None = None
    table_tag: str | None = None
    # 표 본문(list[dict] — {tag,caption,markdown,html}). kind="table" 일 때 원소 1개,
    # kind="chunk" 면 None. References 가 markdown/HTML 로 렌더(answer_renderer).
    # frozen dataclass eq/hash 제외(compare=False) — list[dict] 는 unhashable.
    tables: list[dict[str, Any]] | None = field(default=None, compare=False)


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
        # 통합 cite-N 풀(spec_driven_table_citation_granularity D2) — chunk 마다 본문
        # cite 1개 + 그 chunk 가 보유한 표 개수만큼 table cite. 단일 카운터(_n)로 번호를
        # 매겨 본문/표가 같은 [cite-N] 공간을 공유한다(본문에서 [n] 로 표시·검증 동일).
        # 배치 순서: chunk 본문 cite 직후 그 chunk 의 표 cite 들(parent 인접·가독성).
        candidates_list: list[CitationCandidate] = []
        n = 0
        for c in chunks:
            cid = f"cite-{n}"
            n += 1
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
                    kind="chunk",
                    parent_chunk_id=c.chunk_id,
                    # 본문 cite 는 표를 자동 렌더하지 않는다(표는 아래 table cite 로 분리).
                    tables=None,
                )
            )
            # 표 cite — 그 chunk 의 표 중 **본문이 [TABLE: tag] 로 실제 가리키는 표만**
            # 독립 후보로 승격한다(spec_driven_table_citation_granularity — 본문 미참조
            # 표 제외). 인덱싱 시 같은 chunk 에 묶였어도 본문이 안 가리키면 컨텍스트·
            # References 에 싣지 않는다(과도한 context·무관 표 노출 차단). 본문 소스는
            # capture_mode 와 정합(full=text 전문, 그 외=snippet) — render 가 보는 것과
            # 같은 본문에서 마커를 읽어야 한다. 출처 메타는 parent chunk 승계(D3).
            # markdown·html 둘 다 빈 표는 건너뛴다(인용 실체 없음).
            ref_body = c.text if (self._capture_mode == "full" and c.text) else c.snippet
            referenced_tags = _referenced_table_tags(ref_body)
            for entry in (c.tables or ()):
                if not isinstance(entry, dict):
                    continue
                tag = entry.get("tag")
                # 본문 마커가 가리키지 않는 표는 제외(tag 없는 표도 참조 불가 → 제외).
                if not tag or tag not in referenced_tags:
                    continue
                if not (entry.get("markdown") or entry.get("html") or "").strip():
                    continue
                t_cid = f"cite-{n}"
                n += 1
                candidates_list.append(
                    CitationCandidate(
                        citation_id=t_cid,
                        chunk_id=c.chunk_id,
                        document_id=c.document_id,
                        page=c.page,
                        score=c.score,
                        doc_type=dt,
                        section=c.section,
                        revision=c.revision,
                        response_date=c.response_date,
                        formatted=format_table_citation(c, t_cid, entry),
                        source_url=c.source_url,
                        clause_id=c.clause_id,
                        kind="table",
                        parent_chunk_id=c.chunk_id,
                        table_tag=entry.get("tag"),
                        tables=[entry],
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
        """N4 생성 컨텍스트 렌더(spec_driven_table_citation_granularity).

        `# CONTEXT` — kind="chunk" 후보의 본문(표 마커는 제거). 표는 본문에서 분리해
        모델이 표 근거와 본문 근거를 섞지 않게 한다(D1).
        `# TABLES` — kind="table" 후보. 각 표를 고유 `[cite-N]` 블록(caption+표)으로 제시.
        후보↔chunk 정렬은 parent_chunk_id→chunk dict 조회(1:1 zip 가정 해체 — D5)."""
        sections: list[str] = []
        if pack.conversation_summary:
            sections.append(f"# CONVERSATION_SUMMARY\n{pack.conversation_summary}")

        by_chunk_id = {c.chunk_id: c for c in pack.chunks}

        # --- # CONTEXT: 본문 후보(kind="chunk") -----------------------------
        body_lines: list[str] = []
        for cand in pack.citation_candidates:
            if cand.kind != "chunk":
                continue
            chunk = by_chunk_id.get(cand.parent_chunk_id or cand.chunk_id)
            head = cand.formatted or (
                f"[{cand.citation_id}] {cand.document_id}#{cand.chunk_id} (p={cand.page})"
            )
            if chunk is None:
                body_lines.append(f"{head}\n(chunk unavailable)")
                continue
            if self._capture_mode == "full" and chunk.text:
                body = chunk.text
            elif self._capture_mode in ("snippets", "full") and chunk.snippet:
                body = chunk.snippet
            else:
                body = "(metadata-only capture)"
            # 표 마커 제거 — 표는 # TABLES 의 독립 cite 로 분리됐다(본문에 인라인 치환
            # 하지 않는다, D1). 마커는 silent 삭제가 아니라 *분리* 이므로 표 자체는
            # # TABLES 에 그대로 보인다(원칙 6 — 누락 아님).
            body = _strip_table_markers(body)
            body_lines.append(f"{head}\n{body}")
        sections.append("\n\n".join(body_lines) if body_lines
                        else "(no retrieved context)")

        # --- # TABLES: 표 후보(kind="table"), 있을 때만 ----------------------
        table_lines: list[str] = []
        for cand in pack.citation_candidates:
            if cand.kind != "table" or not cand.tables:
                continue
            entry = cand.tables[0]
            rendered = _render_table_entry(entry)
            if rendered is None:
                continue
            # 출처(문서·페이지)를 헤더에 병기 — 표가 어느 chunk 에서 왔는지 가시.
            src = f"{cand.document_id or '?'}"
            if cand.page is not None:
                src += f", p. {cand.page}"
            table_lines.append(f"[{cand.citation_id}] (표 — {src})\n{rendered}")
        if table_lines:
            sections.append("# TABLES\n" + "\n\n".join(table_lines))

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
