from __future__ import annotations

from typing import Any

from app.application.retrieval.corpus_map import CorpusMap
from app.application.terminology.vocab import TerminologyVocab
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class ConfidenceScopeTool:
    """react_minimal_v1 `confidence.scope` — 모델 주도 라우팅용 *근거 있는 자기진단*.

    분류기가 없는 ReAct variant 에서 소형 모델에게 두 신호를 결정론적으로 돌려준다:
      (a) 질의가 도메인/코퍼스 범위 안인가 — CorpusMap 룰 매칭(코퍼스 coverage),
      (b) 질의 용어를 얼마나 이해하는가 — 용어집(ISO 25964) coverage + 미해소 용어.

    핵심: 이 도구는 **노출만 한다(게이트 아님)**. coverage 를 계산해 모델이 메워야 할
    규제지식 *공백*(unresolved_terms)을 드러낼 뿐, 라우팅 *결정*은 하지 않는다 — 모델이
    이 신호를 보고 canonicalize/expand/search 로 공백을 메우고, 최종 scope 판정은
    submit_response 로 *표현*한다(표현=모델 / 결정=코드, [[feedback_model_over_rule]]).
    corpus_map/vocab 미배치(dev/test)면 default() 폴백 — coverage 0, signal=uncertain.

    재현성: vocab_sha / corpus_map_hash 를 출력에 실어 "어떤 어휘·코퍼스맵으로 이
    coverage 가 나왔나"를 단독 설명한다(원칙 5)."""

    name = "confidence.scope"
    version = "v1"

    def __init__(
        self,
        *,
        corpus_map: CorpusMap | None = None,
        vocab: TerminologyVocab | None = None,
        tau_high: float = 0.6,
        tau_low: float = 0.3,
    ) -> None:
        self._corpus_map = corpus_map or CorpusMap.default()
        self._vocab = vocab or TerminologyVocab.default()
        self._tau_high = tau_high
        self._tau_low = tau_low

    async def invoke(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        data = dict(tool_input or {})
        entities = data.get("entities") or {}
        # terms 우선, 없으면 entities 값 평탄화(노형명·규제 ID 등).
        terms = [str(t) for t in (data.get("terms") or []) if str(t).strip()]
        if not terms and isinstance(entities, dict):
            terms = [
                str(t)
                for vals in entities.values()
                for t in (vals or [])
                if str(t).strip()
            ]

        # --- 용어 coverage(용어집 결정론 lookup) ---
        canon = self._vocab.canonicalize(terms)
        unresolved = list(canon.unresolved)
        resolved = [t for t in canon.canonical_terms if t not in set(unresolved)]
        term_coverage = (
            round(len(resolved) / len(terms), 3) if terms else 0.0
        )

        # --- 코퍼스 coverage(CorpusMap 룰 매칭) ---
        # confidence=tau_high 로 둬 룰이 매칭되면 낮은 confidence 게이트에 막히지 않고
        # filter/boost scope 를 받는다(이 도구는 "매칭 여부"만 본다, 게이트 아님).
        scope = self._corpus_map.resolve_scope(
            scenario_object=context.scenario_object,  # 분류기 없음 → None
            scenario_depth=context.scenario_depth,
            intents=tuple(data.get("intents") or ()),
            entities=entities if isinstance(entities, dict) else {},
            confidence=self._tau_high,
            tau_high=self._tau_high,
            tau_low=self._tau_low,
        )
        collections = _scope_collections(scope.target, scope.filters)

        # --- 자문 signal(coarse 문자열 — 게이트 아님, 모델이 추론) ---
        if scope.matched_rule_id and term_coverage >= 0.5:
            signal = "in_scope_high"
        elif scope.matched_rule_id or canon.concept_ids:
            signal = "in_scope_low_terms"
        else:
            signal = "uncertain"

        output = {
            "term_coverage": term_coverage,
            "resolved_terms": resolved,
            "unresolved_terms": unresolved,
            "concept_ids": list(canon.concept_ids),
            "corpus_match": {
                "matched_rule_id": scope.matched_rule_id,
                "collections": collections,
            },
            "known_collections": sorted(self._corpus_map.collections.keys()),
            "signal": signal,
            "vocab_sha": self._vocab.vocab_sha,
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


def _scope_collections(
    target: dict[str, list[str]], filters: dict[str, Any]
) -> list[str]:
    """ScopeDecision target/filters 의 collection 값을 평탄화(자문용 표시). 값은
    인덱스 keyword(collection/search_type)라 문자열만 모은다(중복 제거·정렬)."""
    out: list[str] = []
    for src in (target or {}, filters or {}):
        for v in src.values():
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, (list, tuple)):
                out.extend(str(x) for x in v)
    return sorted(dict.fromkeys(out))
