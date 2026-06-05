from __future__ import annotations

import hashlib
import json

from app.domain.finder import AnswerSlot, AnswerSpec
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort

_TRACER = get_tracer("intake")

# agentic_finder N2 — "답변 사양" 인스턴스화. 답에 *진짜 필요한 정보*(요구 슬롯)와
# 답 합성 *구조* 를 질의별로 정의한다. 룰 사상표가 아니라 *모델*(utility LLM +
# json_schema grammar, temperature 0)이 산출한다 — InformationNeedInstantiator 와
# 동형. 실패(파싱불가/미가용) 시에만 결정론 fallback(intent prior). 어느 경로였는지
# `instantiation_method` 로 기록(silent degrade 금지). 표현=모델 / 결정=코드 분리:
# 여기선 답변 사양을 *표현*만 하고, 검색 충족 판정은 downstream(Finder LLM)이 소유한다.
#
# 프롬프트·스키마·model_options 는 코드 인라인이 아니라 prompts/registry.yaml 의
# answer_spec_prompts 블록에서 관리되며 AnswerSpecPromptSource 가 sha 검증 후 주입한다.

# intent → 필수 슬롯 prior. *fallback 전용 lookup* — 모델 부재/실패 시에만 쓰인다.
_SLOT_PRIOR: dict[str, tuple[str, ...]] = {
    "definition": ("definition", "governing_clause"),
    "feature": ("design_feature", "requirement_text"),
    "causal": ("requirement_text", "design_feature"),
    "procedural": ("requirement_text", "applicability"),
    "comparison": ("design_feature", "requirement_text"),
    "compliance": ("governing_clause", "requirement_text", "applicability", "effective_version"),
    "permissibility": ("governing_clause", "condition_exception"),
    "verification": ("governing_clause", "requirement_text"),
    "advisory": ("requirement_text", "design_feature"),
    "exploratory": ("definition", "design_feature"),
}
_DEFAULT_SLOTS: tuple[str, ...] = ("governing_clause", "requirement_text")


class AnswerSpecInstantiator:
    """N2 — 답변 사양을 모델로 인스턴스화. 프롬프트·스키마는 registry 에서 주입
    (AnswerSpecPromptSource.build_instantiator)."""

    version = "answer_spec/v1"

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

    async def instantiate(
        self,
        query_text: str,
        *,
        scenario_object: str | None,
        scenario_depth: str | None,
        intent: str | None,
        entities: dict[str, list[str]] | None = None,
    ) -> AnswerSpec:
        # .replace (not .format): 프롬프트 본문에 JSON 예시의 { } 가 있어 .format 은
        # KeyError. LLMClassifier/InformationNeedInstantiator 와 동일 idiom.
        prompt = (
            self._prompt
            .replace("{intent}", intent or "unknown")
            .replace("{object}", scenario_object or "?")
            .replace("{depth}", scenario_depth or "?")
            .replace("{query}", query_text)
        )
        with _TRACER.start_as_current_span("intake.answer_spec") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            if self._policy_hash:
                span.set_attribute("answer_spec.policy_hash", self._policy_hash)
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
                if parsed is not None and parsed[0]:
                    slots, structure = parsed
                    spec = _build(slots, structure, scenario_depth, "llm",
                                  self._policy_hash)
                    span.set_attribute("answer_spec.method", "llm")
                    span.set_attribute("answer_spec.num_slots", len(slots))
                    oi.set_io(span, output_value={
                        "method": "llm", "num_slots": len(slots),
                        "answer_structure": structure,
                    })
                    return spec
            except Exception:  # noqa: BLE001 — 미가용/파싱불가 → 결정론 fallback
                pass
            slots = _prior_slots(intent)
            spec = _build(slots, None, scenario_depth, "fallback", self._policy_hash)
            span.set_attribute("answer_spec.method", "fallback")
            span.set_attribute("answer_spec.num_slots", len(slots))
            oi.set_io(span, output_value={"method": "fallback", "num_slots": len(slots)})
            return spec


def _parse(text: str) -> tuple[tuple[AnswerSlot, ...], str | None] | None:
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
    raw_slots = data.get("required_slots")
    if not isinstance(raw_slots, list):
        return None
    slots: list[AnswerSlot] = []
    for s in raw_slots:
        if isinstance(s, dict):
            name = str(s.get("name") or "").strip()
            desc = str(s.get("description") or "").strip()
            required = bool(s.get("required", True))
        else:
            name, desc, required = str(s).strip(), "", True
        if name:
            slots.append(AnswerSlot(name=name, description=desc, required=required))
    structure_raw = data.get("answer_structure")
    structure = str(structure_raw).strip() if structure_raw else None
    return tuple(slots), structure


def _prior_slots(intent: str | None) -> tuple[AnswerSlot, ...]:
    names = _SLOT_PRIOR.get((intent or "").lower(), _DEFAULT_SLOTS)
    return tuple(AnswerSlot(name=n, required=True) for n in names)


def _build(
    slots: tuple[AnswerSlot, ...],
    structure: str | None,
    depth: str | None,
    method: str,
    policy_hash: str | None,
) -> AnswerSpec:
    canon = (
        "|".join(f"{s.name}:{int(s.required)}" for s in slots)
        + "||" + (structure or "")
        + "||" + (depth or "")
    )
    spec_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    return AnswerSpec(
        required_slots=tuple(slots),
        answer_structure=structure,
        depth=depth,
        instantiation_method=method,
        spec_hash=spec_hash,
        policy_hash=policy_hash,
    )
