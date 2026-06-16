from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# spec_driven_v1 — 검색 *앞단* 두 모델 노드(Define Spec → Query Formulation)의 도메인 모델.
# 설계: docs/plans/spec_driven_agent.design.v1.md.
#
# finder.py / retrieval.py 와 동일 idiom: frozen dataclass(pydantic 아님). 재현성 필드가
# `InteractionEvent` 로 `dataclasses.asdict()` 되며, 그 함수는 dataclass/dict/list/tuple
# 만 재귀하므로 pydantic 모델을 두면 repr 로 stringify 된다(domain/query.py 주석 참조).
#
# 본 모듈은 legacy(finder.AnswerSpec)와 *별개* 다 — 필드 구성이 다르다(explicit_references,
# 슬롯별 keywords, governing_normative_class). 동명 클래스가 있으나 모듈 격리로 충돌 없음;
# spec_driven 코드는 본 모듈에서만 import 한다.


@dataclass(frozen=True)
class SpecSlot:
    """답을 *방어 가능하게* 떠받칠 근거 조각 하나. `keywords` 는 검색 쿼리의 lexical
    앵커(리터럴 보존 — 정규화 금지, F1). N2 QueryFormulator 가 슬롯당 검색쿼리 1개를
    만들 때 이 keywords 를 query_text 로 옮긴다(BM25 lexical).

    `facet` 은 이 슬롯이 *어떤 종류*의 근거를 회수하는지의 라벨이다(값/결론이 아니라 종류
    — address-not-content 불변, project_answer_spec_address_not_content). 답변 심도 설계
    (spec_driven_answer_depth.design.v1 §3.2)의 핵심: N1 이 한 개념을 정의·개별기준·적용·
    정량한계·예외·상호참조 등 *수직* facet 으로 펼쳐 각자 슬롯화하면, N2 가 그 유형에 맞춰
    쿼리를 빚고(§4) N4 가 그 facet 을 어떻게 표현할지(값이면 verbatim, 열거면 리스트) 안다.
    optional(미지정 None 허용 — 하위호환). frozen dataclass 라 asdict 재귀 호환(기존 idiom).

    `expected_authority` 는 이 facet 근거가 *어느 권위/문서군*에 사는지의 힌트다(예:
    definition→정의조항, review_finding→SER/RAI). N2 가 collection filter/boost 를 고를 때
    보조 신호로 쓴다(권위 분리 — 답변 심도 §4). 역시 라벨이지 값이 아니다."""

    name: str
    keywords: tuple[str, ...] = ()
    description: str = ""
    required: bool = True
    # 회수 근거의 *종류* 라벨(값 아님). definition | criterion | applicability |
    # quantitative_limit | method | design_claim | review_finding | exception |
    # cross_reference | None. N2 쿼리 형태·N4 표현형의 신호(spec_driven_answer_depth §3.2).
    facet: str | None = None
    # 이 facet 이 사는 권위/문서군 힌트(N2 collection 선택 보조 — 답변 심도 §4). 라벨.
    expected_authority: str | None = None


@dataclass(frozen=True)
class AnswerSpec:
    """N1 Define Spec Node 산출 — "답변 사양". 검색 전, 원질의에서 *무엇을 근거로*
    (slots), *명시적으로 지칭된 문서/조문*(explicit_references — 리터럴 보존), *어떤
    권위로 anchor*(governing_normative_class), *어떤 논리 구조로 합성*(answer_structure)
    을 정한다.

    N2 의 입력 계약(무엇을 쿼리로)이자 N4 Generation 의 컨텍스트 동반물(어떤 구조·권위로
    합성)이다. `spec_hash` 는 재현성 핀(canonical 문자열 sha16), `policy_hash` 는 정적
    프롬프트 sha16. `instantiation_method`("llm"|"fallback")로 silent degrade 방지."""

    intent: str = "unknown"
    # 질의에 *명시적으로 지칭된* 문서·조문(예: "10 CFR 50.46", "RG 1.157", "GDC 35").
    # 리터럴 보존 — N2 가 적어도 한 쿼리의 query_text 에 verbatim 으로 싣는다(BM25 lexical).
    explicit_references: tuple[str, ...] = ()
    required_slots: tuple[SpecSlot, ...] = ()
    answer_structure: str | None = None  # 예: "정의→지배조문→요건→예외".
    # binding | guidance | review_record | applicant_claim | mixed | null.
    # 답을 anchor 할 권위 등급(권위 인플레이션 방지 — 생성 프롬프트 ladder).
    governing_normative_class: str | None = None
    # 주제 라벨(멀티턴 topic-shift 감지용 — session_memory 설계 §6.5). 모델이 1줄로
    # 산출(표현=모델). 게이트가 prior/current topic_label 동등성으로 주제 전환을 본다.
    # 단일턴/미산출 시 None. 라벨이지 값/근거가 아니다(address-not-content 불변).
    topic_label: str | None = None
    instantiation_method: str = "stub"  # "llm" | "fallback" | "stub"
    spec_hash: str | None = None
    policy_hash: str | None = None


@dataclass(frozen=True)
class TriageDecision:
    """N0 Triage Node 산출 — 라우팅 판정(설계 spec_driven_general_query_routing.design.v1).

    `route` 는 **소형 모델 단독**으로 낸다(결정론 룰/정규식 없음 — 사용자 결정 G2):
    질의가 코퍼스 근거 없이 도메인 추론으로 *방어 가능*하면 `general`, 특정 조문·문서·
    정량값·개정판·신청자 주장을 지칭/요구하면 `retrieval`. 코드는 이 값을 *교정하지
    않는다*. `references_specifics` 는 모델이 채우는 자기검증 신호(결정론 게이트 아님 —
    감사·CoT 구조화용). `triage_method`("llm"|"fallback")로 silent degrade 방지 —
    fallback 은 모델 응답 파싱불가 시 안전 기본값(retrieval)이지 라우팅 규칙이 아니다."""

    route: str = "retrieval"  # "retrieval" | "general"
    references_specifics: bool = True  # 안전 기본값(불확실=특정성 있음=retrieval).
    rationale: str = ""
    triage_method: str = "stub"  # "llm" | "fallback" | "stub"
    policy_hash: str | None = None


@dataclass(frozen=True)
class FormulatedQuery:
    """N2 Query Formulation Node 산출 — 슬롯 1개에 대한 구체 검색쿼리(per-slot, 설계 §3.2).

    `query_text` 는 BM25 lexical 앵커(슬롯 keywords 리터럴 + 관련 explicit_reference
    토큰 verbatim). `target` 는 boost-scope(collection 가산만 — recall-safe). `filters` 는
    hard-scope(모델이 `collection_mode=filter` 를 골랐을 때 — 모집단을 좁힘). 한 쿼리는
    실무상 둘 중 하나만 collection 을 싣는다(모델이 mode 를 택하므로). `references` 는 이
    쿼리에 합류된 명시적 참조(감사용). dict-of-list 라 `dataclasses.asdict()` 재귀 호환."""

    slot_name: str
    query_text: str
    target: dict[str, list[str]] = field(default_factory=dict)  # boost {"collection": [...]}
    filters: dict[str, Any] = field(default_factory=dict)  # hard-scope {"collection": [...]}
    references: tuple[str, ...] = ()
