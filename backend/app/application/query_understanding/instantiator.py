from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.domain.query import RequirementSlot, SubQuestion
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort

_TRACER = get_tracer("query")

# v1 Node 3 — information_need 인스턴스화. 답변에 *진짜 필요한 정보*(요구 슬롯)를
# 질의별로 정의한다. 룰 사상표가 아니라 *모델*(utility LLM + json_schema grammar,
# temperature 0)이 산출한다 — ClaimDecomposer(Node 14)와 동형. 실패(파싱불가/미가용)
# 시에만 결정론 fallback(intent prior). 어느 경로였는지 `method` 로 기록(silent
# degrade 금지). 표현=모델 / 결정=코드 분리: 여기선 요구를 *표현*만 하고, 슬롯 충족
# 판정·게이트는 downstream(Node 6) 결정론 코드가 소유(아직 미배선).
#
# 문서: docs/plans/information_need_driven_retrieval.plan.v1.md §2·§4·§5.

# intent → 필수 슬롯 prior. *fallback 전용 lookup* — 모델 부재/실패 시에만 쓰인다.
# 모델 경로는 이 prior 를 벗어나 질의 특수성(복합 조건·다중 조문·암묵 예외)을 반영.
_SLOT_PRIOR: dict[str, tuple[str, ...]] = {
    "definition": ("definition", "source_clause", "scope"),
    "feature": ("requirement_text", "applicability"),
    "causal": ("requirement_text", "rationale"),
    "procedural": ("procedure_steps", "precondition"),
    "comparison": ("comparison_dimension", "requirement_text"),
    "compliance": ("governing_clause", "requirement_text", "applicability", "effective_version"),
    "permissibility": ("governing_clause", "condition_exception", "authority"),
    "verification": ("governing_clause", "requirement_text"),
    "status_change": ("current_vs_changed", "effective_version"),
    "advisory": ("requirement_text", "rationale"),
    "exploratory": ("definition", "scope"),
}
_DEFAULT_SLOTS: tuple[str, ...] = ("governing_clause", "requirement_text")

# 구조화 출력 스키마(classifier_output_v1.json 과 동형 — 모델 강제 디코딩 directive).
# guided-decoding 미지원 어댑터에선 no-op 이고 파싱이 강제력을 대신한다(port 계약).
NEED_SCHEMA = {
    "type": "object",
    "properties": {
        "required_slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "required": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
        "sub_questions": {"type": "array", "items": {"type": "string"}},
        "version_constraint": {"type": ["string", "null"]},
        "multi_intent": {"type": "boolean"},
    },
    "required": ["required_slots"],
}

_PROMPT = (
    "너는 SMR 인허가 도메인 질의의 *정보 요구(information need)*를 정의하는 분석기다. "
    "주어진 질의에 *방어 가능한 답*을 하려면 어떤 정보 조각(슬롯)이 근거로 있어야 "
    "하는지 판단하라. 규제 답변의 후보 슬롯: governing_clause(지배 조문) · "
    "requirement_text(요건 본문) · applicability(적용 범위) · condition_exception"
    "(조건·예외) · effective_version(발효·개정) · authority(권위) · definition(정의). "
    "질의 특수성(복합 조건·다중 조문·암묵 예외)을 반영해 슬롯을 가감하라. 질의가 "
    "여러 독립 물음을 담으면 sub_questions 로 분해하고 multi_intent=true. 특정 "
    "시점·개정을 못박으면 version_constraint(YYYY-MM-DD), 아니면 null. JSON 만 출력.\n\n"
    "intent: {intent}\nobject/depth: {object}/{depth}\n질의: {query}\n"
)


@dataclass(frozen=True)
class InstantiateResult:
    required_slots: tuple[RequirementSlot, ...]
    sub_questions: tuple[SubQuestion, ...]
    version_constraint: str | None
    multi_intent: bool
    method: str  # "llm" | "fallback"
    prompt_hash: str  # 재현 핀(프롬프트 본문 sha16)
    information_need_hash: str  # 산출 fingerprint(pack_hash 류, 재현 핀 아님)


class InformationNeedInstantiator:
    """Node 3 — 정보 요구를 모델로 인스턴스화(ClaimDecomposer 동형)."""

    version = "information_need/v1"

    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    async def instantiate(
        self,
        query_text: str,
        *,
        scenario_object: str | None,
        scenario_depth: str | None,
        intent: str | None,
        entities: dict[str, list[str]] | None = None,
    ) -> InstantiateResult:
        prompt = _PROMPT.format(
            intent=intent or "unknown",
            object=scenario_object or "?",
            depth=scenario_depth or "?",
            query=query_text,
        )
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        with _TRACER.start_as_current_span("query.information_need") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            span.set_attribute("information_need.prompt_hash", prompt_hash)
            try:
                res = await self._llm.generate(
                    prompt,
                    model_options={"temperature": 0.0},
                    grammar=GrammarSpec(kind="json_schema", value=NEED_SCHEMA),
                )
                oi.set_llm(
                    span, model_name=res.model_id, prompt=prompt, completion=res.text,
                    prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(res.token_usage.get("completion_tokens", 0)),
                )
                parsed = _parse(res.text)
                if parsed is not None and parsed[0]:
                    slots, subqs, vc, mi = parsed
                    out = _result(slots, subqs, vc, mi, "llm", prompt_hash)
                    span.set_attribute("information_need.method", "llm")
                    span.set_attribute("information_need.num_slots", len(slots))
                    span.set_attribute("information_need.num_sub_questions", len(subqs))
                    return out
            except Exception:  # noqa: BLE001 — 미가용/파싱불가 → 결정론 fallback
                pass
            slots = _prior_slots(intent)
            out = _result(slots, (), None, False, "fallback", prompt_hash)
            span.set_attribute("information_need.method", "fallback")
            span.set_attribute("information_need.num_slots", len(slots))
            oi.set_io(span, output_value={"method": "fallback", "num_slots": len(slots)})
            return out


def _parse(
    text: str,
) -> tuple[tuple[RequirementSlot, ...], tuple[SubQuestion, ...], str | None, bool] | None:
    text = (text or "").strip()
    # grammar 미적용 백엔드가 코드펜스·서두를 붙일 수 있어 관대하게 추출
    # (ClaimDecomposer._parse_llm_claims 와 동일 idiom).
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
    slots: list[RequirementSlot] = []
    for s in raw_slots:
        if isinstance(s, dict):
            name = str(s.get("name") or "").strip()
            required = bool(s.get("required", True))
        else:
            name, required = str(s).strip(), True
        if name:
            slots.append(RequirementSlot(name=name, required=required))
    subqs: list[SubQuestion] = []
    for i, q in enumerate(data.get("sub_questions") or []):
        txt = (str(q.get("text") or "") if isinstance(q, dict) else str(q)).strip()
        if txt:
            subqs.append(SubQuestion(id=f"sq{i}", text=txt))
    vc_raw = data.get("version_constraint")
    vc = str(vc_raw).strip() if vc_raw else None
    multi = bool(data.get("multi_intent", len(subqs) > 1))
    return tuple(slots), tuple(subqs), vc, multi


def _prior_slots(intent: str | None) -> tuple[RequirementSlot, ...]:
    names = _SLOT_PRIOR.get((intent or "").lower(), _DEFAULT_SLOTS)
    return tuple(RequirementSlot(name=n, required=True) for n in names)


def _result(
    slots: tuple[RequirementSlot, ...],
    subqs: tuple[SubQuestion, ...],
    vc: str | None,
    multi: bool,
    method: str,
    prompt_hash: str,
) -> InstantiateResult:
    canon = (
        "|".join(f"{s.name}:{int(s.required)}" for s in slots)
        + "||" + "|".join(q.text for q in subqs)
        + "||" + (vc or "")
    )
    need_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    return InstantiateResult(
        required_slots=tuple(slots),
        sub_questions=tuple(subqs),
        version_constraint=vc,
        multi_intent=multi,
        method=method,
        prompt_hash=prompt_hash,
        information_need_hash=need_hash,
    )
