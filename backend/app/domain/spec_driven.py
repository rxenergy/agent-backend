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
    """답을 *방어 가능하게* 떠받칠 근거 조각 하나이자, 답변의 한 *논리 구획*. 슬롯은 검색의
    단위(슬롯당 쿼리 1개)이자 생성의 단위(슬롯당 구획 1개)다.

    책임 재분배(spec_driven_answer_spec_query_responsibility_split.design.v1): N1 은 *답변
    설계자* — 슬롯이 답의 무엇을(facet)·전체에서 어떤 역할을(role)·어느 선행 슬롯 위에서
    (depends_on)·어느 깊이로(depth)·무슨 개념을 찾을지(scope_hint)를 정한다. *검색 주소
    번역*(어느 collection·어떤 토큰·canonical_id)은 N2 QueryFormulator 의 책임이다 — N1 은
    reg ID 사전·BM25 토큰을 더 이상 만들지 않는다.

    `facet` — 이 슬롯이 *어떤 종류*의 근거를 회수하는지의 라벨(값/결론 아닌 종류 —
    address-not-content 불변, project_answer_spec_address_not_content). 인허가 추론 사슬의 한
    층(requirement→acceptance_criterion→demonstration_method→applicant_design→
    review_finding→open_item_condition (+technical_basis·exemption_departure·applicability·
    definition·cross_reference)). **N1 이 소유한다**(facet=답변 분해 논리). N2 가 이 라벨을
    검색 collection 으로 *번역*하고, N4 가 facet 을 어떻게 표현할지(값이면 verbatim+기술근거,
    주장↔판단 분리) 안다. optional(None 허용 — 하위호환). frozen 이라 asdict 재귀 호환.

    `role` — 이 슬롯이 *전체 답에서* 맡는 역할/목표 1문장(N1 산출). N4 슬롯 생성이 독립 LLM
    콜이라도 자기 역할을 매번 추론하지 않게 하는 일관성 장치(설계 §4.3). 예: "이후 슬롯들의
    판단 기준이 되는 지배 요건을 확립".

    `depends_on` — 이 슬롯 생성이 *논리적으로* 참조해야 할 선행 슬롯 name 들. N4 가 PRIOR
    SECTIONS 를 위치(직전 K개)가 아니라 *논리 의존*으로 전달하는 근거이자, 병렬 스케줄링의
    DAG 간선(의존 없는 슬롯끼리 병렬 생성 가능). 입증 사슬은 자연히 사슬 의존, 병렬 개념(예:
    5개 허용기준)은 의존 없음. 사이클 금지(AnswerSpec.slot_graph 가 위상정렬 시 검증).

    `depth` — 이 슬롯을 얼마나 깊게 전개할지(`shallow`|`standard`|`deep`). archetype(질의
    유형)이 결정 — definition 질의=shallow, technical_basis/demonstration=deep. N4 표현 심도 신호.

    `scope_hint` — *개념 수준*의 검색 의도(예: "최대 피복재 온도 허용기준과 그 기술적 근거").
    reg ID·정량값을 적지 않는다 — 그 번역(주소·토큰)은 N2 책임. address-not-content 불변.

    `keywords`(deprecated) — v1 호환 잔존. N1 이 더는 채우지 않는다. N2 는 `scope_hint`
    우선, 비어 있으면 `keywords` 로 fallback(회귀 0). v2 검증 후 제거.

    `expected_authority`(deprecated) — facet 에서 N2 가 문서군을 도출하므로 잉여. v1 호환
    잔존, v2 검증 후 제거."""

    name: str
    # N1 책임 — 답변 설계.
    facet: str | None = None  # 회수 근거의 *종류* 라벨(N1 소유, N2 가 collection 으로 번역).
    role: str = ""  # 전체 답에서 이 슬롯의 역할/목표 1문장(일관성 장치 — 설계 §4.3).
    depends_on: tuple[str, ...] = ()  # 논리 선행 슬롯 name(PRIOR 전달·DAG 간선).
    depth: str = "standard"  # shallow | standard | deep — 전개 심도(archetype 결정).
    scope_hint: str = ""  # 개념 수준 검색 의도(reg ID/값 아님 — N2 가 주소로 번역).
    description: str = ""  # 생성이 surface 할 sub-point(질의 언어 가능). 값/결론 금지.
    required: bool = True
    # --- deprecated(v1 호환 잔존, v2 검증 후 제거) ---
    keywords: tuple[str, ...] = ()  # N1 더는 안 채움. N2 가 scope_hint 없을 때 fallback.
    expected_authority: str | None = None  # facet 에서 N2 가 도출 — 잉여.


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

    def slot_order(self) -> tuple[SpecSlot, ...]:
        """슬롯의 *생성 순서* — `depends_on` DAG 위상정렬(의존 슬롯이 먼저)
        (책임 재분배 설계 §4.1). 같은 위상층에서는 N1 산출 순서를 보존한다(required 가
        먼저 오도록 한 N1 의도 유지 — 재정렬 룰 없음, 표현=모델). depends_on 이 모두 비어
        있으면 산출 순서 그대로(=v1 동형). 사이클이나 미존재 의존은 *무시*하고 산출 순서로
        fallback 한다(silent degrade 금지 — 호출부가 graceful 하게 선형 진행)."""
        slots = list(self.required_slots)
        by_name = {s.name: s for s in slots}
        # 미존재 의존은 간선에서 제외(graceful). 존재하는 의존만 indegree 계산.
        deps = {
            s.name: {d for d in s.depends_on if d in by_name and d != s.name}
            for s in slots
        }
        ordered: list[SpecSlot] = []
        placed: set[str] = set()
        remaining = list(slots)  # 산출 순서 보존(같은 층 내 안정 정렬).
        # Kahn 변형 — 매 패스에서 "의존이 모두 배치된" 슬롯을 산출 순서대로 배치.
        progressed = True
        while remaining and progressed:
            progressed = False
            still: list[SpecSlot] = []
            for s in remaining:
                if deps[s.name] <= placed:
                    ordered.append(s)
                    placed.add(s.name)
                    progressed = True
                else:
                    still.append(s)
            remaining = still
        # 사이클로 남은 슬롯(의존이 영영 안 풀림)은 산출 순서로 뒤에 붙인다(fallback).
        ordered.extend(remaining)
        return tuple(ordered)


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
    토큰 verbatim). `target` 는 boost-scope(collection/status/design/canonical_id 가산 —
    recall-safe). `filters` 는 hard-scope(모델이 `*_mode=filter` 를 골랐을 때 — 모집단을
    좁힘). status↔규제 / design↔NuScale 배타성에 어긋나거나 canonical_id 게이트를 통과
    못해 *무시된* 채널은 `scope_audit` 에 남긴다(silent drop 금지 — 원칙 6). `references`
    는 이 쿼리에 합류된 명시적 참조(감사용). dict-of-list 라 `dataclasses.asdict()` 재귀 호환."""

    slot_name: str
    query_text: str
    target: dict[str, list[str]] = field(default_factory=dict)  # boost scope 필드들
    filters: dict[str, Any] = field(default_factory=dict)  # hard-scope 필드들
    references: tuple[str, ...] = ()
    # 배타성 위반/게이트 기각으로 무시된 채널 기록(재현 핀 입력). 빈 dict 기본 → net-neutral.
    # 예: {"status_dropped": True, "design_dropped": False, "canonical_id_rejected": True}.
    scope_audit: dict[str, Any] = field(default_factory=dict)
