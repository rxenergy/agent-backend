from __future__ import annotations

from dataclasses import dataclass, field

# v3.1 (hierarchical_corrective) — Phase A Query Understanding domain models.
#
# These are frozen dataclasses (not pydantic) because `QueryPlan` reproducibility
# fields are surfaced into `InteractionEvent` via `dataclasses.asdict()`, which
# only recurses into dataclasses / dict / list / tuple. A pydantic model left in
# an event field would be stringified to its repr by `json.dumps(default=str)`.
# See `domain/interaction.py` ToolCallRecord for the established pattern.


@dataclass(frozen=True)
class SubQuestion:
    """One decomposed sub-question (Node 3, multi-intent path)."""

    id: str
    text: str
    entities: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RequirementSlot:
    """Node 3 — 답을 *방어 가능하게* 구성하려면 근거로 있어야 할 정보 조각 하나
    (예: governing_clause / requirement_text / condition_exception / effective_version).
    모델이 질의별로 인스턴스화한다(룰 사상표 아님 — 문서 §2.2).

    `satisfied` 는 Node 3 시점엔 항상 False(요구는 *표현*만, 충족 판정은 Node 6
    요구 커버리지 단계 소유 — 아직 미배선). 표현=모델 / 결정=코드 분리."""

    name: str
    required: bool = True
    satisfied: bool = False


@dataclass(frozen=True)
class QueryPlan:
    """Node 3 output — normalized entities, optional sub-questions, intents,
    and the version (effective_on) constraint that downstream Hard gate (G3)
    and Claim version-match step consume.

    `decompose_prompt_hash` is populated only when the multi-intent LLM
    decomposition actually ran — its presence in the event records that an
    LLM call occurred at this node (reproducibility).

    v1(model-based) 추가: `required_slots` 는 모델이 인스턴스화한 정보 요구
    슬롯(기록 전용 — Node 6/9 소비는 아직 미배선). `instantiation_method`
    ("llm"|"fallback") 는 silent degrade 방지(어느 경로였나). `information_need_hash`
    는 *산출 fingerprint*(pack_hash 류)지 재현 *핀*이 아니다 — 핀은
    `decompose_prompt_hash`(프롬프트 본문 sha)."""

    sub_questions: tuple[SubQuestion, ...] = ()
    normalized_entities: dict[str, list[str]] = field(default_factory=dict)
    intents: tuple[str, ...] = ()
    version_constraint: str | None = None  # e.g. "2024-06-01" (effective_on)
    multi_intent: bool = False
    ner_dict_version: str | None = None
    normalizer_version: str | None = None
    decompose_prompt_hash: str | None = None
    # v1 model-based information need (Node 3). 기록 전용(Node 6/9 미소비).
    required_slots: tuple[RequirementSlot, ...] = ()
    instantiation_method: str | None = None  # "llm" | "fallback"
    information_need_hash: str | None = None
