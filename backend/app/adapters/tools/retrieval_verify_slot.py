"""retrieval.verify_slot — 슬롯 1개의 1차 검색 결과를 Node2 LLM 으로 검증(spec_driven_v2).

Hexagonal 구조:
  - Tool 프로토콜 구현 (RetrievalVerifySlotTool)
  - Port 의존: SlotVerifierPort (DI로 주입 — 구현체는 Node2 LLMPort 기반)
  - Domain I/O: VerifySlotInput → VerifySlotResult

이 도구는 슬롯 **1개**를 처리한다(per-slot fan-out 은 러너 spec_driven_v2 가 수행).
동시성 캡(_MAX_CONCURRENCY)은 러너가 슬롯들을 동시에 invoke 할 때 단일 Node2 vLLM 의
KV-cache 경쟁을 막는다(retrieval.follow_up 과 동일 취지 — verify·follow_up 이 같은 Node2
를 공유한다).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.domain.retrieval import VerifySlotInput, VerifySlotResult
from app.domain.tools import ToolResult
from app.ports.slot_verifier import SlotVerifierPort
from app.ports.tool import ToolExecutionContext

_log = structlog.get_logger("retrieval.verify_slot")


class RetrievalVerifySlotTool:
    """retrieval.verify_slot 도구. Tool 프로토콜 구현.

    Port 의존: SlotVerifierPort (DI로 주입).
    """

    name = "retrieval.verify_slot"
    version = "v1"

    # 동시 검증 슬롯 수 상한. 러너가 슬롯별로 이 도구를 동시 invoke 할 때 단일 Node2
    # vLLM 의 KV-cache 경쟁을 막는다(follow_up 과 동일 — 무제한 동시 발사 시 청크당
    # 디코딩이 느려져 per-call 타임아웃·재시도 캐스케이드). ceil(슬롯수/conc) 라운드로 직렬화.
    _MAX_CONCURRENCY = 3

    def __init__(self, *, slot_verifier: SlotVerifierPort,
                 max_concurrency: int | None = None) -> None:
        self._verifier = slot_verifier
        self._sem = asyncio.Semaphore(max_concurrency or self._MAX_CONCURRENCY)

    async def invoke(
        self,
        tool_input: VerifySlotInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = VerifySlotInput.model_validate(tool_input)

        _log.info(
            "verify_slot_start",
            slot_name=tool_input.slot_name,
            num_chunks=len(tool_input.chunks),
            interaction_id=context.interaction_id,
        )

        async with self._sem:
            res = await self._verifier.verify_slot(
                query_text=tool_input.query_text,
                answer_spec=tool_input.answer_spec,
                slot_name=tool_input.slot_name,
                slot_query=tool_input.slot_query,
                chunks=[c.model_dump(mode="json") for c in tool_input.chunks],
            )

        output = VerifySlotResult.model_validate(res)
        _log.info(
            "verify_slot_done",
            slot_name=tool_input.slot_name,
            num_necessary=len(output.necessary_chunk_ids),
            num_multihop=len(output.multihop_chunk_ids),
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
