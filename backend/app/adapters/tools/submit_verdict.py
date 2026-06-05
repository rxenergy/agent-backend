from __future__ import annotations

from typing import Any

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class SubmitVerdictTool:
    """agentic_finder `submit_verdict` — Finder 루프 종료/충분성 판정 캡처(설계
    llm_tool_calling §5). **no-op tool**: 실행 부작용 없이 인자(verdict)를 그대로
    output 으로 echo 한다 — 종료가 free-text 가 아니라 *구조화 도구 호출*이라 §7
    계측(verdict_sufficient/missing_slots/reason)이 항상 깨끗한 입력을 받는다.

    "도구는 통제된다"(registry+ToolExecutor)를 유지: submit_verdict 도 동일 executor
    경로로 라우팅돼 timeout/span/output_hash 가 부여되고 사이드채널에 기록된다."""

    name = "submit_verdict"
    version = "v1"

    async def invoke(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        data = dict(tool_input or {})
        output = {
            "sufficient": bool(data.get("sufficient", False)),
            "missing_slots": list(data.get("missing_slots") or []),
            "reason": str(data.get("reason") or ""),
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
