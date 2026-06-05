from __future__ import annotations

from typing import Any

from app.application.retrieval.corpus_map import CorpusMap
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class RetrievalScopeTool:
    """agentic_finder `retrieval.scope` — 검색 범위 파라미터 생성(설계 finder §3).
    **결정론** — v3.1 `CorpusMap.resolve_scope` 를 차용해 (scenario_object/entities/
    intents) 로 target(boost)/filters(hard)/min_token_count 를 산출한다. Finder LLM 은
    이 출력을 `retrieval.search` 인자로 전달한다("어느 도구를 언제"만 LLM, 범위 계산은
    코드 — 표현=모델 / 결정=코드 분리, [[feedback_model_over_rule]]).

    confidence 는 분류 confidence 게이트 입력인데 Finder 도구 호출 시점엔 LLM 이
    주입하지 않으므로 `default_confidence`(기본 tau_high — rule 매칭 시 filter 강도)로
    둔다. 튜너블. corpus_map 미배치(dev/test)면 default() → mode="off"."""

    name = "retrieval.scope"
    version = "v1"

    def __init__(
        self,
        *,
        corpus_map: CorpusMap | None = None,
        tau_high: float = 0.6,
        tau_low: float = 0.3,
        default_confidence: float = 0.6,
        min_token_count: int = 0,
    ) -> None:
        self._corpus_map = corpus_map or CorpusMap.default()
        self._tau_high = tau_high
        self._tau_low = tau_low
        self._default_confidence = default_confidence
        self._min_token_count = min_token_count

    async def invoke(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        data = dict(tool_input or {})
        entities = data.get("entities") or {}
        intents = tuple(data.get("intents") or ())
        confidence = float(data.get("confidence", self._default_confidence))
        scope = self._corpus_map.resolve_scope(
            scenario_object=context.scenario_object,
            scenario_depth=context.scenario_depth,
            intents=intents,
            entities=entities,
            confidence=confidence,
            tau_high=self._tau_high,
            tau_low=self._tau_low,
            settings_min_token_count=self._min_token_count,
        )
        output = {
            "mode": scope.mode,
            "target": scope.target,
            "filters": scope.filters,
            "min_token_count": scope.min_token_count,
            "matched_rule_id": scope.matched_rule_id,
            "corpus_map_hash": scope.corpus_map_hash,
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
