from __future__ import annotations

import json
from typing import Any

from app.domain.spec_driven import TriageDecision
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort

_TRACER = get_tracer("intake")

# spec_driven_v1 N0 — Triage Node(라우팅 판정). 원질의를 보고 답을 *방어*하려면 코퍼스
# 근거가 필요한지(`retrieval`) 아니면 모델 도메인 추론으로 충분한지(`general`)를 **소형
# 모델 단독**으로 판정한다(설계 spec_driven_general_query_routing.design.v1, 사용자 결정
# G2 — 결정론 룰/정규식 없음). 코드는 모델이 낸 route 를 교정하지 않는다.
#
# 안전 비대칭(retrieval→general 오분류 = 규제 사실 날조 = 치명)에 대한 방어는 결정론
# net 이 아니라 *프롬프트*(retrieval 편향 규칙 + 경계 few-shot, registry 호스팅)와 N4-G
# 범위 한정 날조 가드에 있다. 본 노드의 유일한 코드 안전망은 *파싱불가 시 degradation*:
# 모델 응답이 없으면 라우팅할 근거가 없으므로 retrieval(자체 gap-answer 가드 보유)로
# 안전 degrade 한다 — 라우팅 규칙이 아니라 최후 기본값.
#
# 프롬프트·스키마·model_options 는 코드 인라인이 아니라 prompts/registry.yaml 의
# spec_driven_triage_prompts 블록에서 관리되며 SpecDrivenTriageSource 가 sha 검증 후 주입한다.

_ROUTES = frozenset({"retrieval", "general"})


class SpecDrivenTriage:
    """N0 — Triage Node. 프롬프트·스키마는 registry 에서 주입(SpecDrivenTriageSource).
    N1/N2 instantiator 와 동형(LLM + json_schema + 관대 파싱 + degradation)."""

    version = "spec_driven/triage/v1"

    def __init__(
        self,
        llm: LLMPort,
        *,
        prompt_body: str,
        schema: dict | None = None,
        model_options: dict | None = None,
        policy_hash: str | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt_body
        self._schema = schema
        self._model_options = dict(model_options or {"temperature": 0.0})
        self._policy_hash = policy_hash

    async def triage(self, query_text: str) -> TriageDecision:
        # .replace (not .format): 프롬프트 본문 few-shot 의 JSON 예시 { } 가 .format 에서
        # KeyError. N1/N2 와 동일 idiom.
        prompt = self._prompt.replace("{query}", query_text)
        with _TRACER.start_as_current_span("intake.spec_driven_triage") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            if self._policy_hash:
                span.set_attribute("triage.policy_hash", self._policy_hash)
            try:
                grammar = (
                    GrammarSpec(kind="json_schema", value=self._schema)
                    if self._schema else None
                )
                res = await self._llm.generate(
                    prompt, model_options=dict(self._model_options), grammar=grammar,
                )
                oi.set_llm(
                    span, model_name=res.model_id, prompt=prompt, completion=res.text,
                    prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(res.token_usage.get("completion_tokens", 0)),
                )
                parsed = _parse(res.text)
                if parsed is not None:
                    decision = TriageDecision(
                        route=parsed["route"],
                        references_specifics=parsed["references_specifics"],
                        rationale=parsed["rationale"],
                        triage_method="llm",
                        policy_hash=self._policy_hash,
                    )
                    span.set_attribute("triage.route", decision.route)
                    span.set_attribute("triage.method", "llm")
                    oi.set_io(span, output_value={
                        "route": decision.route,
                        "references_specifics": decision.references_specifics,
                    })
                    return decision
            except Exception:  # noqa: BLE001 — 미가용/파싱불가 → 안전 degrade(retrieval)
                pass
            # degradation(라우팅 규칙 아님): 모델 출력 없음 → retrieval(안전 기본값).
            decision = TriageDecision(
                route="retrieval", references_specifics=True,
                rationale="triage 응답 파싱불가 — 안전 degrade(retrieval)",
                triage_method="fallback", policy_hash=self._policy_hash,
            )
            span.set_attribute("triage.route", decision.route)
            span.set_attribute("triage.method", "fallback")
            oi.set_io(span, output_value={"route": decision.route, "method": "fallback"})
            return decision


def _parse(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    # grammar 미적용 백엔드가 코드펜스·서두를 붙일 수 있어 관대하게 추출.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    route = str(data.get("route") or "").strip().lower()
    if route not in _ROUTES:
        # route 누락/오타 = 라우팅 근거 불명 → 안전하게 retrieval(교정 아니라 degrade).
        route = "retrieval"
    refs = data.get("references_specifics")
    references_specifics = bool(refs) if isinstance(refs, bool) else True
    rationale = str(data.get("rationale") or "").strip()
    return {
        "route": route,
        "references_specifics": references_specifics,
        "rationale": rationale,
    }
