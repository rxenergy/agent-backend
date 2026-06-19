"""retrieval.rescope — none_necessary 슬롯의 검색 스코프를 Node2 LLM 으로 재계획.

Hexagonal 구조:
  - Tool 프로토콜 구현 (RetrievalRescopeTool)
  - Port 의존: RescopePort (DI로 주입 — 구현체는 Node2 LLMPort 기반)
  - Domain I/O: RescopeInput → RescopeResult

이 도구는 슬롯 **1개**를 처리한다(per-slot fan-out 은 러너가 수행). verify_slot 이
none_necessary 로 판정한 슬롯에서만 호출된다(verify→{follow_up|rescope}→verify 분기 교체).
동시성 캡(_MAX_CONCURRENCY)은 러너가 슬롯들을 동시 invoke 할 때 단일 Node2 vLLM 의
KV-cache 경쟁을 막는다(verify/follow_up 과 동일 취지 — 셋이 같은 Node2 를 공유).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.domain.retrieval import RescopeInput, RescopeResult
from app.domain.tools import ToolResult
from app.ports.rescope import RescopePort
from app.ports.tool import ToolExecutionContext

_log = structlog.get_logger("retrieval.rescope")


class RetrievalRescopeTool:
    """retrieval.rescope 도구. Tool 프로토콜 구현.

    Port 의존: RescopePort (DI로 주입).
    """

    name = "retrieval.rescope"
    version = "v1"

    # 동시 재계획 슬롯 수 상한. 러너가 슬롯별로 이 도구를 동시 invoke 할 때 단일 Node2
    # vLLM 의 KV-cache 경쟁을 막는다(verify/follow_up 과 동일 — 무제한 동시 발사 시 per-call
    # 타임아웃·재시도 캐스케이드). ceil(슬롯수/conc) 라운드로 직렬화.
    _MAX_CONCURRENCY = 3

    def __init__(self, *, rescoper: RescopePort,
                 max_concurrency: int | None = None) -> None:
        self._rescoper = rescoper
        self._sem = asyncio.Semaphore(max_concurrency or self._MAX_CONCURRENCY)

    async def invoke(
        self,
        tool_input: RescopeInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RescopeInput.model_validate(tool_input)

        _log.info(
            "rescope_start",
            slot_name=tool_input.slot_name,
            interaction_id=context.interaction_id,
        )

        async with self._sem:
            res = await self._rescoper.rescope(
                query_text=tool_input.query_text,
                answer_spec=tool_input.answer_spec,
                slot_name=tool_input.slot_name,
                slot_query=tool_input.slot_query,
                why_not_needed=tool_input.why_not_needed,
                what_is_needed=tool_input.what_is_needed,
                initial_scope=dict(tool_input.initial_scope),
                max_queries=tool_input.max_queries,
            )

        output = RescopeResult.model_validate(res)
        _log.info(
            "rescope_done",
            slot_name=tool_input.slot_name,
            num_queries=len(output.queries),
            method=output.method,
            interaction_id=context.interaction_id,
        )
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
