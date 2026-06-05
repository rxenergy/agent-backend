from __future__ import annotations

from typing import Any

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext

# 원자력/SMR 인허가 도메인 용어 → 정규형·정의 사전(결정론 lookup).
#
# ⚠️ PROVISIONAL SEED — 완전 사전이 아니다(소수 시드 항목). 사용자 결정(F-4 라운드):
# normalize 는 *결정론 매핑*(약어→정규형)이지 추출/정의 *생성*이 아니므로 룰 lookup
# 으로 둔다([[feedback_model_over_rule]]의 "표현=모델"은 추출/정의에 적용; 매핑은
# 예외). 미등록 용어는 passthrough(원형 보존). 추후 운영 사전으로 확장.
_TERM_DICT: dict[str, dict[str, str]] = {
    "eccs": {"normalized": "ECCS", "definition": "비상노심냉각계통(Emergency Core Cooling System)"},
    "ecc": {"normalized": "ECCS", "definition": "비상노심냉각계통(Emergency Core Cooling System)"},
    "l-smr": {"normalized": "i-SMR", "definition": "혁신형 소형모듈원자로(innovative SMR)"},
    "ismr": {"normalized": "i-SMR", "definition": "혁신형 소형모듈원자로(innovative SMR)"},
    "rai": {"normalized": "RAI", "definition": "추가정보요청(Request for Additional Information)"},
    "fsar": {"normalized": "FSAR", "definition": "최종안전성분석보고서(Final Safety Analysis Report)"},
    "rcs": {"normalized": "RCS", "definition": "원자로냉각재계통(Reactor Coolant System)"},
    "loca": {"normalized": "LOCA", "definition": "냉각재상실사고(Loss-of-Coolant Accident)"},
}


class RetrievalNormalizeTool:
    """agentic_finder `retrieval.normalize` — 원자력 도메인 용어 정규화·정의(설계
    finder §3). **결정론 사전 lookup**(사용자 결정). Finder LLM 이 후보 용어를 넘기면
    정규형·정의를 돌려주고, 미등록 용어는 원형 그대로 passthrough(silent drop 금지)."""

    name = "retrieval.normalize"
    version = "v1"

    def __init__(self, *, term_dict: dict[str, dict[str, str]] | None = None) -> None:
        self._dict = term_dict if term_dict is not None else _TERM_DICT

    async def invoke(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        data = dict(tool_input or {})
        terms = data.get("terms") or []
        normalized: list[str] = []
        definitions: dict[str, str] = {}
        unresolved: list[str] = []
        for raw in terms:
            term = str(raw or "").strip()
            if not term:
                continue
            hit = self._dict.get(term.lower())
            if hit:
                norm = hit["normalized"]
                normalized.append(norm)
                definitions[norm] = hit["definition"]
            else:
                normalized.append(term)  # passthrough (원형 보존).
                unresolved.append(term)
        output = {
            "normalized_terms": normalized,
            "definitions": definitions,
            "unresolved_terms": unresolved,
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
