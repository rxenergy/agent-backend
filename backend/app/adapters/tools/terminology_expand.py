from __future__ import annotations

from typing import Any

from app.application.terminology.vocab import TerminologyVocab
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class TerminologyExpandTool:
    """agentic_finder `terminology.expand` — 시소러스(ISO 25964) 결정론 검색범위 확장
    (설계 docs/plans/terminology_normalization_strategy.v1.md §3.3).

    **Finder-LLM-invoked, recover 전용** — 첫 검색이 불충분할 때 용어의 동의어(UF)·
    하위어(NT)로 검색 범위를 넓힌다(재현율↑). RT(관련어)는 affinitive 라 주제 이탈
    위험이 커 opt-in. 첫 검색 전 사용 금지는 *Finder 루프의 코드 게이트*(searched_once)가
    강제한다 — 도구 자체는 무상태 결정론 lookup. 통제어휘=정밀(canonicalize) /
    시소러스=재현(expand) 분리(통제어휘는 N1.5 conductor)."""

    name = "terminology.expand"
    version = "v1"

    def __init__(self, *, vocab: TerminologyVocab) -> None:
        self._vocab = vocab

    async def invoke(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        data = dict(tool_input or {})
        terms = data.get("terms") or []
        relations = data.get("relations") or ("uf", "nt")
        max_per_term = data.get("max_per_term")
        result = self._vocab.expand(
            terms,
            relations=tuple(relations),
            max_per_term=int(max_per_term) if max_per_term else None,
        )
        output = {
            "expanded_terms": list(result.expanded_terms),
            "relations": {k: list(v) for k, v in result.relations.items()},
            "vocab_sha": self._vocab.vocab_sha,
        }
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output,
            latency_ms=0,
            input_hash="",  # filled by executor
            trace_id=context.trace_id,
        )
