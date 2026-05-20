from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class CitationCheckInput(BaseModel):
    answer_text: str
    citation_ids: list[str]
    chunk_ids: list[str]
    referenced_citation_ids: list[str] = []
    resolvable_citation_ids: list[str] | None = None


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
        provided = set(tool_input.citation_ids)
        referenced = set(tool_input.referenced_citation_ids)
        resolvable = (
            set(tool_input.resolvable_citation_ids)
            if tool_input.resolvable_citation_ids is not None
            else provided
        )
        usable = provided & resolvable
        matched = referenced & usable
        missing = referenced - usable
        # Empty referenced/provided is a *verification outcome* (answer didn't
        # cite anything), not a tool failure. Report completeness=0.0 so the
        # runner's threshold branch can decide FAIL/PARTIAL.
        denom = max(1, len(referenced)) if referenced else 1
        completeness = len(matched) / denom if referenced else 0.0
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={
                "citation_completeness": round(completeness, 3),
                "missing_citation_count": len(missing),
                "matched_citation_ids": sorted(matched),
                "unresolved_citation_ids": sorted(missing),
            },
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
