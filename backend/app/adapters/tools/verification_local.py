from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class CitationCheckInput(BaseModel):
    answer_text: str
    citation_ids: list[str]
    chunk_ids: list[str]


class FaithfulnessCheckInput(BaseModel):
    answer_text: str
    chunk_ids: list[str]


class LocalCitationCheckTool:
    name = "verification.citation_check"
    version = "v1"

    async def invoke(
        self,
        tool_input: CitationCheckInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = CitationCheckInput.model_validate(tool_input)
        has_citation = len(tool_input.citation_ids) > 0
        completeness = 1.0 if has_citation else 0.0
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success" if has_citation else "failed",
            output={
                "citation_completeness": completeness,
                "missing_citation_count": 0 if has_citation else 1,
            },
            error_code=None if has_citation else "tool_empty_result",
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )


class LocalFaithfulnessCheckTool:
    name = "verification.faithfulness_check"
    version = "v1"

    async def invoke(
        self,
        tool_input: FaithfulnessCheckInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = FaithfulnessCheckInput.model_validate(tool_input)
        # 청크 0개면 근거 없음 → 0.0. 그 외에는 0.6 base + 0.1/chunk.
        # 진짜 faithfulness는 Phase W6에서 Ragas adapter로 교체된다.
        score = 0.0 if not tool_input.chunk_ids else min(
            1.0, 0.6 + 0.1 * len(tool_input.chunk_ids)
        )
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"faithfulness": round(score, 3)},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
