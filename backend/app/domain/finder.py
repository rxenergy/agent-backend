from __future__ import annotations

from dataclasses import dataclass, field

# agentic_finder_v4 — Phase 1(Intake N2) / Phase 2(Retrieval N3) 도메인 모델.
#
# v3.1 query.py / retrieval.py 와 동일 idiom: frozen dataclass(pydantic 아님).
# 재현성 필드가 `InteractionEvent` 로 `dataclasses.asdict()` 되며, 그 함수는
# dataclass/dict/list/tuple 만 재귀하므로 pydantic 모델을 두면 repr 로 stringify
# 된다(domain/query.py QueryPlan 주석 참조).


@dataclass(frozen=True)
class AnswerSlot:
    """N2 답변 사양의 정보 슬롯 하나 — 답을 *방어 가능하게* 구성하려면 근거로
    있어야 할 정보 조각(예: governing_clause / requirement_text / design_feature).

    N3 Finder 의 검색 기준이 된다(무엇을 찾아야 하는가). v3.1 RequirementSlot 과
    역할이 같으나, agentic_finder 는 이 슬롯을 Finder LLM 입력 계약으로 *소비* 한다
    (v3.1 은 기록 전용이었다 — 문서 finder §1)."""

    name: str
    description: str = ""
    required: bool = True


@dataclass(frozen=True)
class AnswerSpec:
    """N2 output — "답변 사양". 어떤 답변을, 어떤 구조·깊이로 생성할지의 계약.

    N3 Finder 의 입력(무엇을 찾아야 하는가)이자 N8 Generation 의 컨텍스트 동반물
    (어떤 구조로 합성할까)이다. `spec_hash` 는 재현성 핀(`answer_spec_hash`,
    문서 finder §5). `instantiation_method`("llm"|"fallback")로 silent degrade
    를 방지한다 — N2 가 stub 인 F-0 에선 "stub".

    `answer_structure`/`depth` 는 분류기 산출(scenario_object/scenario_depth)을
    그대로 흘리거나 모델이 정교화한다(F-2 에서 슬롯 산출 배선)."""

    required_slots: tuple[AnswerSlot, ...] = ()
    answer_structure: str | None = None  # 예: "정의→요건→예외", "비교표".
    depth: str | None = None  # scenario_depth 동형(D1/D2/...).
    instantiation_method: str = "stub"  # "llm" | "fallback" | "stub"
    # `spec_hash` = 산출 fingerprint(pack_hash 류, 재현 *핀* 아님).
    # `policy_hash` = 정적 프롬프트 fragment sha16(재현 핀 = `answer_spec_hash`,
    # classifier_policy_hash 와 동일 idiom). F-6 에서 InteractionEvent 로 승격.
    spec_hash: str | None = None
    policy_hash: str | None = None


@dataclass(frozen=True)
class FinderRound:
    """N3 Finder 루프 1라운드 계측(문서 finder §5). intra-node iteration 을
    InteractionEvent 가 v3.1 수준에서 못 잡으므로 신규 캡처한다 — LLM 단독 검증
    리스크의 사후 audit 입력(런타임 게이트가 아니라 계측+오프라인 audit)."""

    round_index: int
    query: str = ""
    normalized_terms: tuple[str, ...] = ()
    scope_params: dict[str, object] = field(default_factory=dict)
    num_chunks: int = 0
    verdict_sufficient: bool | None = None  # submit_verdict 산출(없으면 None).
    missing_slots: tuple[str, ...] = ()
    verdict_reason: str | None = None
    reranker_score_dist: tuple[float, ...] = ()
