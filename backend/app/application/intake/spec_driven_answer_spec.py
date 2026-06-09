from __future__ import annotations

import hashlib
import json
from typing import Any

from app.application.agents.events import LazyReasoning, current_emitter
from app.application.intake.reasoning_capture import extract_reasoning, stream_capture
from app.domain.spec_driven import AnswerSpec, SpecSlot
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort

_TRACER = get_tracer("intake")

# spec_driven_v1 N1 — "답변 사양" 인스턴스화(Define Spec Node). 원질의에서 의도 +
# 명시적 문서참조(리터럴) + 근거 슬롯(슬롯별 lexical keywords) + 권위 등급 + 논리 구조를
# 산출한다. 룰 사상표가 아니라 *모델*(utility LLM + json_schema grammar, temp 0)이 낸다
# (AnswerSpecInstantiator/InformationNeedInstantiator 와 동형). 실패 시에만 결정론
# fallback(최소 spec). 어느 경로였는지 `instantiation_method` 로 기록(silent degrade 금지).
#
# 프롬프트·스키마·model_options 는 코드 인라인이 아니라 prompts/registry.yaml 의
# spec_driven_answer_spec_prompts 블록에서 관리되며 SpecDrivenAnswerSpecSource 가 sha
# 검증 후 주입한다.

_GOVERNING_CLASSES = frozenset(
    {"binding", "guidance", "review_record", "applicant_claim", "mixed"}
)


class SpecDrivenAnswerSpecInstantiator:
    """N1 — Define Spec Node. 프롬프트·스키마는 registry 에서 주입
    (SpecDrivenAnswerSpecSource)."""

    version = "spec_driven/answer_spec/v1"

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
        self, query_text: str, *, reasoning_label: str | None = None
    ) -> AnswerSpec:
        # .replace (not .format): 프롬프트 본문에 JSON 예시의 { } 가 있어 .format 은
        # KeyError. LLMClassifier/AnswerSpecInstantiator 와 동일 idiom.
        prompt = self._prompt.replace("{query}", query_text)
        with _TRACER.start_as_current_span("intake.spec_driven_answer_spec") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            if self._policy_hash:
                span.set_attribute("answer_spec.policy_hash", self._policy_hash)
            try:
                grammar = (
                    GrammarSpec(kind="json_schema", value=self._schema)
                    if self._schema else None
                )
                # emitter 활성 시 streaming 으로 native CoT 를 thinking 에 흘리고
                # (없으면 구조화 `reasoning` 필드 backstop) — 설계 D2/D3. 비활성(run)
                # 이면 현행 non-stream 그대로(비용 0, 회귀 없음).
                em = current_emitter()
                lazy = LazyReasoning(reasoning_label) if em.active else None
                if lazy is not None:
                    res = await stream_capture(
                        self._llm, prompt,
                        model_options=dict(self._model_options),
                        grammar=grammar, lazy=lazy,
                    )
                else:
                    res = await self._llm.generate(
                        prompt, model_options=dict(self._model_options),
                        grammar=grammar,
                    )
                oi.set_llm(
                    span, model_name=res.model_id, prompt=prompt, completion=res.text,
                    prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(res.token_usage.get("completion_tokens", 0)),
                )
                # native CoT 가 없었으면(소형/Gemma) 구조화 reasoning 필드를 backstop
                # 으로 emit — 노드당 1소스(중복 억제), N4 토큰 이전이라 #24295 안전.
                if lazy is not None and not lazy.emitted:
                    await lazy.feed(extract_reasoning(res.text))
                parsed = _parse(res.text)
                if parsed is not None and parsed["required_slots"]:
                    spec = _build(parsed, "llm", self._policy_hash)
                    span.set_attribute("answer_spec.method", "llm")
                    span.set_attribute("answer_spec.num_slots", len(spec.required_slots))
                    span.set_attribute(
                        "answer_spec.num_refs", len(spec.explicit_references)
                    )
                    oi.set_io(span, output_value={
                        "method": "llm", "intent": spec.intent,
                        "num_slots": len(spec.required_slots),
                        "num_refs": len(spec.explicit_references),
                        "governing_normative_class": spec.governing_normative_class,
                    })
                    return spec
            except Exception:  # noqa: BLE001 — 미가용/파싱불가 → 결정론 fallback
                pass
            spec = _fallback(query_text, self._policy_hash)
            span.set_attribute("answer_spec.method", "fallback")
            span.set_attribute("answer_spec.num_slots", len(spec.required_slots))
            oi.set_io(span, output_value={"method": "fallback"})
            return spec


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
    raw_slots = data.get("required_slots")
    if not isinstance(raw_slots, list):
        return None
    slots: list[SpecSlot] = []
    for s in raw_slots:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        kw = s.get("keywords")
        keywords = tuple(
            str(k).strip() for k in kw if str(k).strip()
        ) if isinstance(kw, list) else ()
        desc = str(s.get("description") or "").strip()
        required = bool(s.get("required", True))
        slots.append(SpecSlot(name=name, keywords=keywords,
                              description=desc, required=required))
    refs_raw = data.get("explicit_references")
    refs = tuple(
        str(r).strip() for r in refs_raw if str(r).strip()
    ) if isinstance(refs_raw, list) else ()
    gnc_raw = data.get("governing_normative_class")
    gnc = str(gnc_raw).strip().lower() if gnc_raw else None
    if gnc not in _GOVERNING_CLASSES:
        gnc = None
    structure_raw = data.get("answer_structure")
    structure = str(structure_raw).strip() if structure_raw else None
    intent = str(data.get("intent") or "unknown").strip() or "unknown"
    return {
        "intent": intent,
        "explicit_references": refs,
        "required_slots": tuple(slots),
        "answer_structure": structure,
        "governing_normative_class": gnc,
    }


def _fallback(query_text: str, policy_hash: str | None) -> AnswerSpec:
    """모델 부재/파싱불가 시 최소 spec — 원질의를 단일 근거 슬롯으로(키워드 보존).
    명시적 참조 추출은 모델 책임이라 fallback 은 refs 비움(억지 추출 금지)."""
    kw = tuple(t for t in query_text.split() if t)[:12]
    slot = SpecSlot(name="primary_evidence", keywords=kw,
                   description="원질의 핵심 근거", required=True)
    parsed = {
        "intent": "unknown",
        "explicit_references": (),
        "required_slots": (slot,),
        "answer_structure": None,
        "governing_normative_class": None,
    }
    return _build(parsed, "fallback", policy_hash)


def _build(parsed: dict[str, Any], method: str, policy_hash: str | None) -> AnswerSpec:
    slots: tuple[SpecSlot, ...] = parsed["required_slots"]
    refs: tuple[str, ...] = parsed["explicit_references"]
    structure = parsed["answer_structure"]
    gnc = parsed["governing_normative_class"]
    intent = parsed["intent"]
    # spec_hash = canonical 문자열 sha16(dict-bearing 인스턴스를 해시하지 않는다 —
    # finder._build 와 동일 규율). 슬롯·keywords·refs·구조·권위·의도를 평탄 직렬화.
    canon = (
        intent
        + "||" + ",".join(refs)
        + "||" + (gnc or "")
        + "||" + (structure or "")
        + "||" + "|".join(
            f"{s.name}:{int(s.required)}:{'+'.join(s.keywords)}" for s in slots
        )
    )
    spec_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    return AnswerSpec(
        intent=intent,
        explicit_references=refs,
        required_slots=slots,
        answer_structure=structure,
        governing_normative_class=gnc,
        instantiation_method=method,
        spec_hash=spec_hash,
        policy_hash=policy_hash,
    )
