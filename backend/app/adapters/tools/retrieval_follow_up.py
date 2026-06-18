"""retrieval.follow_up — 1차 검색 청크에서 외부 참조를 추출하고 재검색 쿼리를 생성.

Hexagonal 구조:
  - Tool 프로토콜 구현 (RetrievalFollowUpTool)
  - Port 의존: RefExtractorPort (DI로 주입)
  - Domain I/O: FollowUpInput → FollowUpResult
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.domain.retrieval import (
    FollowUpInput,
    FollowUpQueryItem,
    FollowUpResult,
)
from app.domain.tools import ToolResult
from app.ports.ref_extractor import RefExtractorPort
from app.ports.tool import ToolExecutionContext

_log = structlog.get_logger("retrieval.follow_up")


class RetrievalFollowUpTool:
    """retrieval.follow_up 도구. Tool 프로토콜 구현.

    Port 의존: RefExtractorPort (DI로 주입)
    """

    name = "retrieval.follow_up"
    version = "v1"

    # 동시 추출 청크 수 상한 — 슬롯 fan-out 전체에 걸친 *전역* 캡(도구가 싱글톤이라
    # self._sem 을 모든 슬롯이 공유). 청크별 추출은 독립 HTTP 요청이므로 동시에 쏘면
    # vLLM 의 continuous batching 이 한 GPU step 에 묶어 처리한다 — 이 batching 을
    # 살리도록 캡을 vLLM max_num_seqs 여유 안에서 넉넉히 둔다(기본 8).
    # 주의: utility vLLM 은 에이전트 본체(N0~N4 생성)와 통합메모리(DGX Spark)를 공유하므로,
    # 캡을 너무 높이면 in-flight 요청 폭주 시 KV cache 경쟁으로 청크당 디코딩이 느려져
    # per-call 타임아웃 → 재시도 캐스케이드가 날 수 있다(과거 무제한 발사 시 실측 91s/8청크).
    # 환경별 최적값은 DOCUMENTS_REF_MAX_CONCURRENCY 로 조정한다(profiles.py).
    _MAX_CONCURRENCY = 8

    def __init__(self, *, ref_extractor: RefExtractorPort,
                 max_concurrency: int | None = None):
        self._extractor = ref_extractor
        self._sem = asyncio.Semaphore(max_concurrency or self._MAX_CONCURRENCY)

    async def invoke(
        self,
        tool_input: FollowUpInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = FollowUpInput.model_validate(tool_input)

        _log.info(
            "follow_up_extract_start",
            num_chunks=len(tool_input.chunks),
            query_text=tool_input.query_text[:80],
            interaction_id=context.interaction_id,
        )

        # 각 chunk 추출을 동시 실행한다. self._extractor 는 async(LLMPort 기반
        # HttpLLM, per-call 독립 HTTP 요청)다. 동시성은 _sem(기본 3)으로 캡한다 —
        # 단일 공유 vLLM 을 에이전트 본체와 함께 쓰는 통합메모리 환경에선 무제한 동시
        # 발사가 KV cache 경쟁으로 청크당 디코딩을 느리게 만들어 per-call 타임아웃·재시도
        # 캐스케이드를 부르기 때문(실측). ceil(N/3) 라운드로 직렬화돼 안정적이다.
        async def _extract_one(chunk) -> list[dict[str, Any]]:
            # 본문 출처: CONTEXT_CAPTURE_MODE=full 일 때만 `text` 가 차고, 평소엔
            # OpenSearch 어댑터가 `snippet`(text[:512]) 만 싣는다. 참조 추출은 본문이
            # 있어야 동작하므로 text → snippet 순으로 폴백한다(둘 다 없으면 빈 문자열).
            chunk_text = chunk.text or chunk.snippet or ""
            # verify_slot 이 이 청크에 부여한 재검색 방향(없으면 None → 기존 동작).
            search_direction = tool_input.search_directions.get(chunk.chunk_id)
            async with self._sem:
                return await self._extractor.extract_follow_ups(
                    query_text=tool_input.query_text,
                    chunk_text=chunk_text,
                    current_source_id=chunk.source_id,
                    min_score=tool_input.min_score,
                    # spec_driven_v2 N3.5 — answer_spec+slot_query 기준 필요-판정 선별
                    # (옵셔널, 미지정 시 v1 전체 추출). 입력 필드 기본값이 v1 byte-identical.
                    answer_spec=tool_input.answer_spec,
                    slot_query=tool_input.slot_query,
                    necessity_only=tool_input.necessity_only,
                    search_direction=search_direction,
                )

        results = await asyncio.gather(
            *(_extract_one(c) for c in tool_input.chunks),
            return_exceptions=True,
        )

        all_follow_ups: list[dict[str, Any]] = []
        for chunk, res in zip(tool_input.chunks, results):
            if isinstance(res, BaseException):
                # 한 청크 추출 실패가 배치 전체를 죽이지 않게 skip(graceful degrade).
                _log.warning(
                    "chunk_extract_failed",
                    chunk_source_id=chunk.source_id,
                    error=str(res),
                    interaction_id=context.interaction_id,
                )
                continue
            _log.debug(
                "chunk_processed",
                chunk_source_id=chunk.source_id,
                num_follow_ups=len(res),
            )
            all_follow_ups.extend(res)

        # 중복 제거 (동일 query_text)
        seen: set[str] = set()
        deduped: list[FollowUpQueryItem] = []
        for fq in all_follow_ups:
            qt = fq.get("query_text", "")
            if qt and qt not in seen:
                seen.add(qt)
                deduped.append(FollowUpQueryItem.model_validate(fq))

        _log.info(
            "follow_up_extract_done",
            total_raw=len(all_follow_ups),
            deduped=len(deduped),
            interaction_id=context.interaction_id,
        )

        output = FollowUpResult(follow_up_queries=deduped)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
