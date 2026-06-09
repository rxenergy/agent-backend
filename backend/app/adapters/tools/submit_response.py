from __future__ import annotations

from typing import Any

from app.application.agents.react_loop import VALID_OUTCOMES, _FALLBACK_OUTCOME
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext

# 종료 신호 어휘(VALID_OUTCOMES/_FALLBACK_OUTCOME)는 react_loop 가 소유한다 — 루프가
# ReAct 종료 프로토콜의 주인이고, adapter 는 그것을 import 해 검증에 쓴다(adapters→
# application 방향, retrieval_scope→corpus_map 과 동형). submit_verdict 와 달리 *scope/
# 명료화* 축을 담아 분류기 없는 모델 주도 라우팅을 지탱한다(표현=모델 / 결정=코드).


class SubmitResponseTool:
    """react_minimal_v1 `submit_response` — ReAct Retrieval 루프 종료 신호 캡처.

    submit_verdict 와 동형 **no-op tool**: 부작용 없이 인자를 정규화해 echo 한다 —
    종료가 free-text 가 아니라 *구조화 도구 호출*이라 conductor 가 항상 깨끗한
    outcome 을 받는다(structured-by-construction). "도구는 통제된다"(registry +
    ToolExecutor) 유지: submit_response 도 동일 executor 경로로 라우팅돼 span/
    output_hash 가 부여된다."""

    name = "submit_response"
    version = "v1"

    async def invoke(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        data = dict(tool_input or {})
        outcome = str(data.get("outcome") or "").strip()
        if outcome not in VALID_OUTCOMES:
            outcome = _FALLBACK_OUTCOME
        output = {
            "outcome": outcome,
            "reason": str(data.get("reason") or ""),
            "missing_info": [str(x) for x in (data.get("missing_info") or [])],
        }
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output,
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
