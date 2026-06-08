from __future__ import annotations

from typing import Any

from app.application.terminology.vocab import TerminologyVocab
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class TerminologyCanonicalizeTool:
    """agentic_finder `terminology.canonicalize` — 용어집(ISO 25964) 결정론 정규화
    (설계 docs/plans/terminology_normalization_strategy.v1.md §3.2).

    **conductor-invoked**(LLM 아님) — N1.5 에서 분류 entities 를 후보 term 으로 넘기면
    surface/uf → preferred 로 정규화하고 정의를 돌려준다. Finder LLM 재량이 아니라
    워크플로우가 *보장* 호출한다(앞선 retrieval.normalize 의 "LLM 이 부를지 말지" 약점
    해소). 미등록 term 은 원형 passthrough(silent drop 금지, 현 normalize 규약 계승).

    **병기(annotate)**: 검색 질의(query_en)는 변형하지 않는다 — 산출한 정규형·정의는
    컨텍스트에 *병기*되어(Finder 시스템 프롬프트 + 생성 컨텍스트) dense 임베딩을 흔들지
    않고 정밀도를 돕는다. 검색범위 확장(동의어/하위어)은 별도 `terminology.expand`
    (Finder recover 전용, P3)."""

    name = "terminology.canonicalize"
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
        result = self._vocab.canonicalize(terms)
        output = {
            "canonical_terms": list(result.canonical_terms),
            "definitions": dict(result.definitions),
            "concept_ids": list(result.concept_ids),
            "unresolved": list(result.unresolved),
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
