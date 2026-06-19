"""슬롯 단위 검색-검증 파이프라인 — spec_driven_v2 / composer_pipelined 공유.

설계: docs/plans/spec_driven_agent.design.v2.md(2-노드 검증) +
docs/plans/spec_driven_slot_pipeline_streaming.design.v1.md(배리어 제거 스트리밍).

한 슬롯의 4-stage 검색 파이프라인을 캡슐화한다:
  Stage1 Node1 검증(retrieval.verify_slot) — 1차 청크 중 필요/멀티홉 식별
  Stage2 Node2 외부참조 선별(retrieval.follow_up, necessity_only) — 멀티홉 청크에서 재검색 대상
  Stage3 Node2 2차 검색(retrieval.search, 참조 문서 내부) — score 게이트 keep_k
  Stage4 Node1 재검증(retrieval.verify_slot) — 2차 청크도 "검색 후엔 항상 relevance"

`_SlotPipelineMixin` 은 위 4-stage 를 `_run_slot_pipeline` 으로 제공하고, 두 변형이 상속한다:
  - **spec_driven_v2** — `_post_retrieval` 시임에서 1차 검색을 *먼저* 전량 돌린 뒤 슬롯별
    청크(slot_chunks)를 이 함수에 넘긴다(검증부터 시작). N4 는 base 단일 생성.
  - **composer_pipelined** — 1차 검색까지 슬롯 future 안으로 넣어(slot_queries) self-contained
    하게 돌리고(`run_slot_search`), 생성 루프가 슬롯별 future 를 소비한다(배리어 제거).

`tool_results` 는 함수 안에서 record 하지 않고 모아서 반환한다 — 호출부(병합 루프 / 생성
루프)가 슬롯 *원순서* 로 순차 record 해 결정성·race 를 보존한다(v1 idiom)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from app.application.agents.composer_base import (
    _NOISE_FILTER,
    _SEARCH_TOOL,
    _parse_chunks,
)
from app.domain.retrieval import RetrievedChunk
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

_VERIFY_TOOL = "retrieval.verify_slot"
_FOLLOW_UP_TOOL = "retrieval.follow_up"
_FETCH_CHUNKS_TOOL = "document.fetch_chunks"  # neighbor_requests 이웃 보강(id 조회)

# chunk_id `<prefix>_c<NNNN>` 의 prefix 와 ordinal 분해(이웃 id 계산용).
_CHUNK_ORD_RE = re.compile(r"^(.*_c)(\d+)$")

# CFR packageId(=source_id)의 연도 추출용. "CFR-1997-title10-vol1" → 1997.
# ref_resolver._CFR_YEAR_RE(adapter)와 같은 발상이나, application 레이어 자족성을
# 위해 여기 작은 정규식을 따로 둔다(adapter import 회피).
_PKG_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _content_key(c: RetrievedChunk) -> str | None:
    """동일내용 판정 키 = 정규화된 본문(text 우선, 없으면 snippet). 공백 collapse +
    lower + strip. 본문이 비면 None(=빈본문끼리 오병합 방지 — 호출부가 고유키로 분리)."""
    body = (c.text or c.snippet or "").strip()
    if not body:
        return None
    return " ".join(body.lower().split())


def _recency_sort_key(c: RetrievedChunk) -> tuple[str, int, float]:
    """최신 우선 정렬 키(max 로 1등 선택). response_date(YYYY-MM-DD 는 사전식==시간순)
    → packageId 연도(source_id) → score 순. 메타 전무면 ("",0,score)로 밀려 score 가
    자연 tiebreak — 동일내용은 score 최고 1개만 남는다(사용자 결정)."""
    date = c.response_date or ""
    m = _PKG_YEAR_RE.search(c.source_id or "")
    year = int(m.group()) if m else 0
    return (date, year, c.score)


def dedupe_latest_version(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """본문 내용이 동일한 청크 그룹마다 최신 버전 1개만 남긴다(예외 안전).

    NRC 코퍼스의 CFR 연도판(CFR-1997-… vs CFR-2025-…)처럼 본문이 글자 그대로 같고
    문서 버전만 다른 중복을, verify_slot LLM 호출 직전에 최신판 1개로 접는다.

      - 그룹핑: _content_key(정규화 본문 완전일치). 빈 본문은 고유키(chunk_id)로 두어
        절대 합쳐지지 않게 한다(빈 텍스트끼리 오병합 방지).
      - 그룹 내 선택: _recency_sort_key 최댓값 1개(response_date→packageId 연도→score).
      - 입력 *원순서* 보존(keep id 집합으로 원리스트 필터)."""
    by_group: dict[str, list[RetrievedChunk]] = {}
    for c in chunks:
        key = _content_key(c) or f"\x00{c.chunk_id}"  # 빈본문 → 고유키
        by_group.setdefault(key, []).append(c)
    keep_ids: set[str] = set()
    for grp in by_group.values():
        winner = max(grp, key=_recency_sort_key)  # date→year→score desc
        keep_ids.add(winner.chunk_id)
    return [c for c in chunks if c.chunk_id in keep_ids]


def compute_neighbor_ids(neighbor_requests: dict[str, str]) -> list[str]:
    """verify_slot 의 neighbor_requests(chunk_id → "before"|"after"|"both")를 실제 이웃
    chunk_id 리스트로 변환한다(결정형 — 모델이 방향만 주고 이웃 id 는 코드가 계산).

    chunk_id 가 `<prefix>_c<NNNN>` 형식일 때만 ordinal±1 로 이웃을 만든다. before→ord-1,
    after→ord+1, both→양쪽. ord-1<0 은 제외, 형식 미매칭 chunk 는 skip. zero-pad 폭은 원본
    자릿수를 유지하되 자릿수가 늘면 자연 확장(9999→10000). 중복 이웃 id 는 한 번만 낸다.
    앵커/타 necessary 와 겹치는 id 는 호출부 dedup(by_id)이 거른다."""
    out: list[str] = []
    seen: set[str] = set()
    for cid, direction in neighbor_requests.items():
        m = _CHUNK_ORD_RE.match(cid or "")
        if not m:
            continue
        prefix, digits = m.group(1), m.group(2)
        ordinal = int(digits)
        width = len(digits)
        targets: list[int] = []
        if direction in ("before", "both"):
            targets.append(ordinal - 1)
        if direction in ("after", "both"):
            targets.append(ordinal + 1)
        for t in targets:
            if t < 0:
                continue
            nid = f"{prefix}{t:0{width}d}"
            if nid != cid and nid not in seen:
                seen.add(nid)
                out.append(nid)
    return out


@dataclass
class _SlotPipelineResult:
    """한 슬롯의 파이프라인 산출(verify→ref_select→second_pass). 병합/소비는 호출부가
    슬롯 원순서대로 *순차* 처리한다(race 방지·결정성 — v1 idiom)."""

    slot_name: str
    method: str                                       # "llm" | "fallback" | "empty"
    num_first_pass: int
    necessary: list[RetrievedChunk]                   # Node1 이 고른 필요 청크
    multihop_ids: list[str]                            # 멀티홉 필요 청크 id
    rationale: str
    fq_list: list[dict[str, Any]] = field(default_factory=list)   # Node2 외부참조 선별 결과
    second_pass: list[RetrievedChunk] = field(default_factory=list)  # Stage4 검증 *통과* 2차 청크
    # Stage 1.5 — neighbor_requests(앞/뒤 문맥 보강)로 가져온 이웃 청크. necessary 와 동급
    # always-include 로 N4 CONTEXT 에 합친다(잘린 본문 보강이므로). by_id/necessary 중복 제외.
    neighbor_chunks: list[RetrievedChunk] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)         # record() 용(순차 기록)
    # Stage 4 — 2차 검색 결과 Node1 재검증. second_pass 는 이미 통과 청크만 담는다(검증 후
    # trim). num_second_pass 는 검증 *입력* 수, second_method 는 재검증 경로
    # ("llm"|"fallback"|"skip"), rationale2 는 재검증 근거(UI thinking 노출용).
    num_second_pass: int = 0
    second_method: str = "skip"
    rationale2: str = ""
    # 이 슬롯 검색-검증 span(agent.slot.<name>)의 context — 생성 루프가 llm.slot_generation
    # span 을 여기에 *link* 로 잇는다(D3). 검색이 생성보다 먼저 떠 부모-자식 불가 →
    # OTel link 로 슬롯 단위 검색→생성 귀인. span 미생성(0건 degrade)이면 None.
    span_context: Any = None


class _SlotPipelineMixin:
    """`_run_slot_pipeline`(4-stage) 을 제공하는 믹스인. 상속하는 러너는
    `self._tools`(ToolExecutor)·`self._follow_up_fetch_k`·`self._follow_up_keep_k`·
    `self._min_token_count` 를 보유해야 한다(ComposerBase 가 모두 제공)."""

    async def _slot_first_pass_search(
        self, *, ctx: ToolExecutionContext,
        slot_queries: list[dict[str, Any]], per_query_k: int,
    ) -> tuple[list[RetrievedChunk], list[Any]]:
        """슬롯의 1차 검색 — 그 슬롯에 귀속된 N2 쿼리들만 동시(gather) 검색·dedup(score-max).
        composer_pipelined 가 1차 검색을 슬롯 future 안으로 넣을 때 쓴다(배리어 제거 — base
        의 전역 직렬 for 루프를 슬롯 단위로 분해). 반환: (슬롯 청크[score desc], tool_results).

        슬롯-로컬 dedup 만 한다(슬롯 간 청크 공유는 호출부가 허용 — 결정성은 호출부의 슬롯
        원순서 소비가 보장). tool_results 는 record 하지 않고 반환(호출부 순차 record)."""
        tool_results: list[Any] = []
        by_id: dict[str, RetrievedChunk] = {}
        outs = await asyncio.gather(
            *(
                self._tools.invoke(  # type: ignore[attr-defined]
                    _SEARCH_TOOL,
                    {"query_text": q["query_text"], "top_k": per_query_k,
                     "target": q.get("target", {}),
                     "min_token_count": self._min_token_count,  # type: ignore[attr-defined]
                     "filters": {**_NOISE_FILTER, **q.get("filters", {})}},
                    ctx,
                )
                for q in slot_queries
            ),
            return_exceptions=True,
        )
        for out in outs:
            if isinstance(out, BaseException):
                continue
            tool_results.append(out)
            found = _parse_chunks(out.output if out.status == "success" else None)
            for c in found:
                prev = by_id.get(c.chunk_id)
                if prev is None or c.score > prev.score:
                    by_id[c.chunk_id] = c
        chunks = sorted(by_id.values(), key=lambda c: c.score, reverse=True)
        return chunks, tool_results

    async def _run_slot_pipeline(
        self, *, request, ctx: ToolExecutionContext, spec_block: str,
        slot_name: str, slot_query: str, slot_chunks: list[RetrievedChunk],
        pre_tool_results: list[Any] | None = None,
    ) -> _SlotPipelineResult:
        """한 슬롯: Node1 검증 → (멀티홉 청크에 대해) Node2 외부참조 선별 → 2차 검색 →
        2차 재검증. tool 결과는 record 하지 않고 모아서 반환한다(호출부가 슬롯 원순서로
        순차 record → 결정성·race 방지). 어떤 단계든 실패/미배선이면 안전 degrade
        (necessary=전량, 멀티홉 없음 → 단일노드 동작과 동형).

        `pre_tool_results` 는 호출부(composer_pipelined)가 슬롯 1차 검색을 이 함수 *밖*에서
        돌렸을 때 그 tool_results 를 앞에 합치기 위한 것(record 순서 보존). v2 는 None.

        슬롯의 4 stage 도구 호출은 여기서 연 `agent.slot.<name>` span 하나의 자식으로 묶인다
        — gather task 마다 contextvars 가 독립이라 슬롯끼리 섞이지 않는다(Phoenix 에서
        agent.run > agent.slot.<name> > {도구} 로 슬롯 단위로 읽힌다)."""
        tool_results: list[Any] = list(pre_tool_results or [])
        # verify LLM 호출 직전 — 본문 동일 청크는 최신판 1개로 접는다(CFR 연도판 등).
        # by_id 부터 일관되게 축소된 집합을 보도록 진입부에서 한 번 dedup.
        slot_chunks = dedupe_latest_version(slot_chunks)
        if not slot_chunks:
            # 0건 슬롯 — verify 호출 낭비 방지(빈 기여). span 도 열지 않는다. 단 1차 검색
            # tool_results(있으면)는 record 되도록 그대로 싣는다.
            return _SlotPipelineResult(
                slot_name=slot_name, method="empty", num_first_pass=0,
                necessary=[], multihop_ids=[], rationale="",
                tool_results=tool_results,
            )

        with _TRACER.start_as_current_span(f"agent.slot.{slot_name}") as slot_span:
            oi.set_kind(slot_span, oi.KIND_CHAIN)
            oi.set_io(slot_span, input_value=slot_query)
            slot_span.set_attribute("slot.name", slot_name)
            slot_span.set_attribute("slot.num_first_pass", len(slot_chunks))

            by_id = {c.chunk_id: c for c in slot_chunks}
            # === Stage 1 — Node1 verify ===
            method = "fallback"
            necessary_ids: list[str] = list(by_id)   # fallback 기본 = 전량 필요
            multihop_ids: list[str] = []
            rationale = ""
            search_directions: dict[str, str] = {}   # 멀티홉 청크 → 재검색 방향
            neighbor_requests: dict[str, str] = {}    # necessary 청크 → 이웃 보강 방향
            try:
                v = await self._tools.invoke(  # type: ignore[attr-defined]
                    _VERIFY_TOOL,
                    {
                        "query_text": request.query_text,
                        "answer_spec": spec_block,
                        "slot_name": slot_name,
                        "slot_query": slot_query,
                        "chunks": [c.model_dump(mode="json") for c in slot_chunks],
                    },
                    ctx,
                )
                tool_results.append(v)
                if v.status == "success" and v.output:
                    method = str(v.output.get("method", "llm"))
                    nids = [i for i in (v.output.get("necessary_chunk_ids") or []) if i in by_id]
                    mids = [i for i in (v.output.get("multihop_chunk_ids") or []) if i in by_id]
                    necessary_ids = nids
                    multihop_ids = mids
                    rationale = str(v.output.get("rationale", ""))
                    # 멀티홉 청크별 재검색 방향(follow_up 에 전달) + 이웃 보강 요청(Stage 1.5).
                    # 키는 각각 multihop/necessary 부분집합으로 한 번 더 제한(어댑터가 이미 필터).
                    search_directions = {
                        k: str(sd)
                        for k, sd in (v.output.get("multihop_search_directions") or {}).items()
                        if k in by_id
                    }
                    neighbor_requests = {
                        k: str(d)
                        for k, d in (v.output.get("neighbor_requests") or {}).items()
                        if k in set(nids)
                    }
            except Exception:  # noqa: BLE001 — ToolUnknown/실패 → 단일노드 degrade(전량 필요).
                method = "fallback"
                necessary_ids = list(by_id)
                multihop_ids = []
                # adapter fallback 과 일관 — 빈 근거 대신 실패 사유를 실어 UI thinking 노출.
                rationale = "⚠ 검증 도구 호출 실패 → 이 슬롯 청크 전량 보존"

            necessary = [by_id[i] for i in necessary_ids if i in by_id]
            multihop = [by_id[i] for i in multihop_ids if i in by_id]

            # === Stage 1.5 — necessary 청크 앞/뒤 문맥 보강(neighbor_requests) ===
            # verify 가 방향만 주면(before/after/both) 코드가 이웃 chunk_id 를 ordinal 로 계산해
            # document.fetch_chunks 로 가져온다. by_id(이 슬롯 1차)·이미 necessary 인 id 는 제외.
            # 미배선/실패는 graceful skip(이웃 없이 진행 — local fake 는 빈 결과).
            neighbor_chunks: list[RetrievedChunk] = []
            neighbor_ids = compute_neighbor_ids(neighbor_requests)
            fetch_ids = [nid for nid in neighbor_ids if nid not in by_id]
            if fetch_ids:
                try:
                    nb = await self._tools.invoke(  # type: ignore[attr-defined]
                        _FETCH_CHUNKS_TOOL,
                        {"chunk_ids": fetch_ids},
                        ctx,
                    )
                    tool_results.append(nb)
                    if nb.status == "success" and nb.output:
                        seen_nb: set[str] = set()
                        for c in _parse_chunks(nb.output):
                            if c.chunk_id in by_id or c.chunk_id in seen_nb:
                                continue
                            seen_nb.add(c.chunk_id)
                            neighbor_chunks.append(c)
                except Exception:  # noqa: BLE001 — 미배선/실패 → 이웃 없이 진행(graceful).
                    neighbor_chunks = []

            # === Stage 2 — Node2 외부참조 선별(enhanced follow_up, necessity_only) ===
            fq_list: list[dict[str, Any]] = []
            if multihop:
                try:
                    fu = await self._tools.invoke(  # type: ignore[attr-defined]
                        _FOLLOW_UP_TOOL,
                        {
                            "query_text": request.query_text,
                            "chunks": [c.model_dump(mode="json") for c in multihop],
                            "answer_spec": spec_block,
                            "slot_query": slot_query,
                            "necessity_only": True,
                            # verify 가 멀티홉 청크별로 부여한 재검색 방향(없으면 빈 → 기존 동작).
                            "search_directions": search_directions,
                        },
                        ctx,
                    )
                    tool_results.append(fu)
                    if fu.status == "success" and fu.output:
                        fq_list = fu.output.get("follow_up_queries", []) or []
                except Exception:  # noqa: BLE001 — graceful skip(2차 검색 없음).
                    fq_list = []

            # === Stage 3 — Node2 2차 검색(참조 문서 내부, score 게이트 keep_k) ===
            second_pass_raw: list[RetrievedChunk] = []
            searchable = [fq for fq in fq_list if fq.get("target_source_ids")]
            if searchable:
                # gather 의 자식 search span 들은 task 생성 시점 context(=이 슬롯 span)를
                # 캡처하므로 모두 슬롯 span 의 자식으로 nesting 된다.
                sub_results = await asyncio.gather(
                    *(
                        self._tools.invoke(  # type: ignore[attr-defined]
                            _SEARCH_TOOL,
                            {
                                "query_text": fq["query_text"],
                                "top_k": self._follow_up_fetch_k,  # type: ignore[attr-defined]
                                "min_token_count": self._min_token_count,  # type: ignore[attr-defined]
                                "filters": {
                                    **_NOISE_FILTER,
                                    "source_id": fq["target_source_ids"],
                                },
                            },
                            ctx,
                        )
                        for fq in searchable
                    ),
                    return_exceptions=True,
                )
                seen: set[str] = set()
                for sub_res in sub_results:
                    if isinstance(sub_res, BaseException):
                        continue
                    tool_results.append(sub_res)
                    found = _parse_chunks(
                        sub_res.output if sub_res.status == "success" else None
                    )
                    for c in found[: self._follow_up_keep_k]:  # type: ignore[attr-defined]
                        if c.chunk_id not in by_id and c.chunk_id not in seen:
                            seen.add(c.chunk_id)
                            second_pass_raw.append(c)

            # === Stage 4 — 2차 검색 결과 Node1 재검증("검색 후엔 항상 relevance") ===
            # 2차 청크를 동일 retrieval.verify_slot 도구로 한 번 더 검증해 *답변에 필요한* 청크만
            # N4 로 보낸다. 멀티홉 출력은 무시한다(3차 홉 없음 — 두번째 Node1 호출 후 바로 N4).
            # 미배선/실패/fallback → 2차 전량 보존(단일노드 degrade). 빈 2차는 스킵(호출 낭비 방지).
            # 2차 검색도 본문 동일 중복을 가질 수 있어 재검증 직전 최신판 1개로 접는다.
            second_pass_raw = dedupe_latest_version(second_pass_raw)
            second_pass = second_pass_raw
            num_second_pass = len(second_pass_raw)
            second_method = "skip"
            rationale2 = ""
            if second_pass_raw:
                sp_by_id = {c.chunk_id: c for c in second_pass_raw}
                second_method = "fallback"  # 기본 = 검증 실패 시 전량 보존
                try:
                    v2 = await self._tools.invoke(  # type: ignore[attr-defined]
                        _VERIFY_TOOL,
                        {
                            "query_text": request.query_text,
                            "answer_spec": spec_block,
                            "slot_name": slot_name,
                            "slot_query": slot_query,
                            "chunks": [c.model_dump(mode="json") for c in second_pass_raw],
                        },
                        ctx,
                    )
                    tool_results.append(v2)
                    if v2.status == "success" and v2.output:
                        second_method = str(v2.output.get("method", "llm"))
                        keep = [i for i in (v2.output.get("necessary_chunk_ids") or [])
                                if i in sp_by_id]
                        rationale2 = str(v2.output.get("rationale", ""))
                        if second_method != "fallback":
                            # 검증 통과 청크만 보존(원순서 유지). fallback 이면 전량 유지.
                            second_pass = [sp_by_id[i] for i in keep if i in sp_by_id]
                except Exception:  # noqa: BLE001 — ToolUnknown/실패 → 2차 전량 보존(degrade).
                    second_method = "fallback"
                    second_pass = second_pass_raw
                    # adapter fallback 과 일관 — 빈 근거 대신 실패 사유를 실어 UI thinking 노출.
                    rationale2 = "⚠ 검증 도구 호출 실패 → 2차 검색 청크 전량 보존"

            # slot span output — CHAIN span 은 자식 처리 후 자기 output 을 구조화 summary 로
            # 단다(intake 노드 패턴). 미설정 시 Phoenix 가 자식 LLM 의 assistant 메시지를
            # 끌어와 표시하므로, 슬롯 처리 결과 요약을 명시한다(verify_pins 와 동일 출처).
            oi.set_io(slot_span, output_value={
                "method": method,
                "num_necessary": len(necessary),
                "num_neighbor": len(neighbor_chunks),
                "num_multihop": len(multihop_ids),
                "num_second_pass": num_second_pass,
                "num_second_necessary": len(second_pass),
                "second_method": second_method,
                "rationale": rationale,
            })
            # 생성 span 이 link 로 이을 수 있게 이 슬롯 검색 span context 를 캡처(D3).
            slot_span_context = slot_span.get_span_context()

        return _SlotPipelineResult(
            slot_name=slot_name, method=method, num_first_pass=len(slot_chunks),
            necessary=necessary, multihop_ids=multihop_ids, rationale=rationale,
            fq_list=fq_list, second_pass=second_pass, neighbor_chunks=neighbor_chunks,
            tool_results=tool_results,
            num_second_pass=num_second_pass, second_method=second_method,
            rationale2=rationale2, span_context=slot_span_context,
        )


@dataclass
class SlotSearchResult:
    """composer_pipelined — 한 슬롯의 검색-검증 완료 결과. 생성 루프가 슬롯별로 소비한다
    (배리어 없음). `_SlotPipelineResult` 를 생성 루프가 쓰기 좋은 형태로 정리한 뷰."""

    slot_name: str
    necessary: list[RetrievedChunk]
    second_pass: list[RetrievedChunk]
    pipeline: _SlotPipelineResult     # 핀/근거/tool_results 원본
    neighbor: list[RetrievedChunk] = field(default_factory=list)  # Stage1.5 이웃 보강 청크

    @property
    def context_chunks(self) -> list[RetrievedChunk]:
        """이 슬롯 생성 CONTEXT = 검증 통과 1차(necessary) ∪ 이웃 보강(neighbor) ∪
        2차(second_pass), score desc."""
        by_id: dict[str, RetrievedChunk] = {}
        for c in (*self.necessary, *self.neighbor, *self.second_pass):
            prev = by_id.get(c.chunk_id)
            if prev is None or c.score > prev.score:
                by_id[c.chunk_id] = c
        return sorted(by_id.values(), key=lambda c: c.score, reverse=True)


class SlotSearchHandle:
    """슬롯명 → Future[SlotSearchResult]. 생성 루프가 슬롯 i 직전 `await result(slot)`.

    설계 §3(spec_driven_slot_pipeline_streaming): 검색·검증은 슬롯별 독립 task 로 N2 직후
    즉시 발사(배리어 없음), 생성 루프는 *생성 순서*(depends_on 위상정렬)로 소비한다 — slot i
    생성 시작 시 slot i task 만 await 하고, 나머지 슬롯 검색은 백그라운드 진행(검색 대기가
    생성 뒤로 숨음). 완료 *순서* 가 아니라 *생성 순서* 로 소비해 결정성을 보존한다.

    현 단계는 직렬 검색 어댑터 — task 가 현 도구를 백그라운드로 감싼다. 외부 노드 병렬 검색이
    준비되면 *같은 인터페이스* 에 병렬 future 를 꽂는다(코드 무변경)."""

    def __init__(self, tasks: dict[str, "asyncio.Task[SlotSearchResult]"]) -> None:
        self._tasks = tasks

    @property
    def slot_names(self) -> list[str]:
        return list(self._tasks)

    async def result(self, slot_name: str) -> SlotSearchResult:
        """슬롯 검색-검증 결과를 기다린다. 미등록 슬롯(검색 0건 등)이면 빈 결과."""
        task = self._tasks.get(slot_name)
        if task is None:
            return SlotSearchResult(
                slot_name=slot_name, necessary=[], second_pass=[],
                pipeline=_SlotPipelineResult(
                    slot_name=slot_name, method="empty", num_first_pass=0,
                    necessary=[], multihop_ids=[], rationale="",
                ),
            )
        return await task

    async def gather_all(self) -> list[SlotSearchResult]:
        """모든 슬롯 결과를 슬롯 등록 순서로 모은다(전량 필요한 마무리 집계용)."""
        return [await self.result(n) for n in self._tasks]

    async def aclose(self) -> None:
        """미소비 슬롯 검색 task 를 정리한다(orphan 방지).

        생성 루프가 조기 return(LLM-unavailable refuse 등)하거나 정상 종료할 때, 아직
        돌고 있는 백그라운드 슬롯 검색 task 가 남으면 (a) asyncio 가 "Task was destroyed
        but it is pending" 경고를 내고, (b) 그 task 안에서 연 `agent.slot.<name>`·도구
        span 이 flush 전에 버려져 Phoenix/Tempo 에서 dangling span 으로 남거나 유실된다.
        호출부가 `finally` 에서 이걸 부르면 미완료 task 를 cancel + 회수해 깔끔히 닫는다.
        결정성과 무관(이미 소비된 결과는 불변 — 미소비 task 만 정리)."""
        pending = [t for t in self._tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            # cancel 후 회수 — CancelledError 포함 모든 예외를 삼킨다(정리 목적이라 개별
            # task 실패는 무관). 이미 완료됐으나 미수확된 task 의 예외도 같이 흡수된다.
            await asyncio.gather(*pending, return_exceptions=True)


@dataclass
class _SlotPack:
    """한 슬롯의 sub-pack + 그 슬롯에 새로 부여된 전역 cite 후보(References 통합용)."""

    pack: Any                         # ContextPack — 전역 번호로 cite-N 이 매겨진 슬롯 서브셋.
    allowed_cites: set[str]           # 이 슬롯 본문이 쓸 수 있는 cite-N(전역, L0 게이트용).
    new_candidates: list[Any]         # 이 슬롯이 *처음* 등장시킨 chunk 의 전역 cite 후보.
    new_chunk_ids: list[str]          # 그 후보들의 chunk_id(retrieved_chunk_ids 누적용).


class SlotCitationAllocator:
    """슬롯 단위 생성에서 cite-N 을 **전역 단일 공간** 으로 관리하는 citation 관리자.

    문제: 슬롯별 sub-pack 을 각자 cite-0 부터 매기면 슬롯1·슬롯2 가 *다른* 근거를 같은
    [cite-0] 으로 표시한다(고객 인용 오류). 사후 재번호는 이미 스트리밍된 토큰을 되돌릴 수
    없어 화면이 틀린다.

    해결(사용자 결정): 슬롯 CONTEXT 로 넘어가기 *전*(프롬프트 렌더 전)에 전역 번호를 배정해
    모델이 처음부터 올바른 전역 [cite-N] 으로 생성하게 한다. 생성은 순차이므로 슬롯을 소비하는
    순서대로 번호를 늘려 결정적이며, 슬롯 검색은 여전히 병렬(번호 배정은 생성 시점이라 검색
    병렬성을 방해하지 않는다).

    중복 제거: 같은 chunk 가 여러 슬롯 CONTEXT 에 들어가면(슬롯 간 공유) *최초 등장 슬롯*의
    전역 cite 를 재사용한다 — 한 근거가 답 전체에서 하나의 [cite-N] 으로 보인다(References
    중복 없음). 재사용 chunk 는 새 번호를 받지 않고 기존 번호로 슬롯 sub-pack 에 들어간다.

    `ContextBuilder` 는 건드리지 않는다 — `build(cite_start=N)` 으로 슬롯 *새* 청크만 전역
    오프셋부터 번호를 매기고, 재사용 청크 후보는 기존 전역 후보를 끼워 넣는다(formatted 포함
    전역 번호 일관)."""

    def __init__(self, context_builder: Any) -> None:
        self._builder = context_builder
        self._next = 0                              # 다음 배정할 전역 cite 번호.
        self._chunk_to_cite: dict[str, str] = {}    # chunk_id → 전역 cite-N(중복 재사용).
        self._all_candidates: list[Any] = []        # 전역 References 통합(부여 순서).
        self._all_chunk_ids: list[str] = []         # 전역 retrieved_chunk_ids(부여 순서).

    @property
    def all_candidates(self) -> list[Any]:
        return self._all_candidates

    @property
    def all_chunk_ids(self) -> list[str]:
        return self._all_chunk_ids

    def build_slot_pack(self, *, chunks: list[RetrievedChunk], **build_kwargs: Any) -> _SlotPack:
        """슬롯 CONTEXT chunk → 전역 번호로 cite-N 이 매겨진 sub-pack. `build_kwargs` 는
        ContextBuilder.build 패스스루(query_text/chat_history/… — chunks/cite_start 제외).

        절차:
          1) 새 chunk(미배정)와 재사용 chunk(기배정) 분리.
          2) 새 chunk 만 cite_start=self._next 로 빌드 → 새 후보가 전역 번호를 받는다.
          3) 재사용 chunk 의 기존 전역 후보를 합쳐 슬롯 sub-pack 후보를 구성(렌더 순서=원
             chunks 순서, score desc 보존).
          4) allowed_cites = 이 슬롯 후보(새+재사용)의 cite-N 전체.
        """
        from dataclasses import replace

        new_chunks = [c for c in chunks if c.chunk_id not in self._chunk_to_cite]
        # 새 chunk 만 전역 오프셋부터 번호 매김(ContextBuilder 가 chunk 본문+표 cite 발급).
        new_pack = self._builder.build(chunks=new_chunks, cite_start=self._next,
                                       **build_kwargs)
        # 새 후보 등록 — chunk 본문 cite 를 chunk_to_cite 에 기록(표 cite 는 parent 본문과
        # 같은 chunk 이나 별도 cite 번호라 본문 cite 만 재사용 키로 쓴다).
        new_cands = list(new_pack.citation_candidates)
        for cand in new_cands:
            if cand.kind == "chunk":
                self._chunk_to_cite[cand.parent_chunk_id or cand.chunk_id] = cand.citation_id
        # 전역 cite 카운터 전진 — 발급된 cite 수만큼(본문+표).
        self._next += len(new_cands)
        new_chunk_ids = [c.chunk_id for c in new_chunks]
        self._all_candidates.extend(new_cands)
        self._all_chunk_ids.extend(new_chunk_ids)

        # 재사용 chunk 의 기존 전역 후보(References 중복 방지 — 새로 안 만든다). 같은
        # parent chunk 의 본문 cite + 표 cite 를 모두 끌어와(여러 cite 가능) 슬롯에 싣는다.
        reused_cands: list[Any] = []
        reused_keys = {c.chunk_id for c in chunks
                       if c.chunk_id in self._chunk_to_cite and c.chunk_id not in new_chunk_ids}
        if reused_keys:
            for gc in self._all_candidates:
                if (gc.parent_chunk_id or gc.chunk_id) in reused_keys:
                    reused_cands.append(gc)

        # 슬롯 sub-pack — 새 후보(전역 번호) + 재사용 후보. 렌더/검수가 이 pack 을 쓴다.
        slot_candidates = tuple(new_cands) + tuple(reused_cands)
        slot_pack = replace(new_pack, citation_candidates=slot_candidates,
                            chunks=tuple(chunks))
        allowed = {c.citation_id for c in slot_candidates}
        return _SlotPack(pack=slot_pack, allowed_cites=allowed,
                         new_candidates=new_cands, new_chunk_ids=new_chunk_ids)
