from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.domain.interaction import ChatTurn


class MemoryType(str, Enum):
    SESSION = "session"
    CANDIDATE = "candidate"
    APPROVED = "approved"


class MemoryReviewStatus(str, Enum):
    CANDIDATE = "candidate"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class StalenessStatus(str, Enum):
    FRESH = "fresh"
    UNKNOWN = "unknown"
    STALE = "stale"


@dataclass(frozen=True)
class MemoryRef:
    memory_id: str
    memory_type: str
    review_status: str
    staleness_status: str
    score: float | None = None


@dataclass
class TrackedReference:
    """대화에서 누적 추적되는 참조 식별자(규제 문서/조문/소스/엔티티). variant-agnostic —
    어떤 variant 든 "대화가 다루는 대상"을 여기 쌓는다(spec_driven=explicit_reference,
    finder=entity/source_id). 연속성 게이트(reference overlap)와 anaphora 해소(prior refs
    동반)의 단위다. `salience` 는 recency·반복으로 커지고 미등장 턴마다 decay 해, 오래된
    참조가 자연 강등되어 무한 누적을 막는다(salience-based eviction).

    설계: docs/plans/spec_driven_session_memory.design.v1.md §1, §3.2, §7."""

    ref_id: str  # "10 CFR 50.46", source_id, entity name 등 리터럴(정규화 금지)
    ref_type: str = "reference"  # "regulation" | "clause" | "source_id" | "entity" | ...
    label: str = ""  # 표시용(옵션)
    first_turn: int = 0
    last_turn: int = 0
    salience: float = 1.0


@dataclass
class RetrievalTrace:
    """한 턴의 검색 결과 식별자 — 재현 + (옵션)후속 검색 scope 힌트. retrieval_history 는
    최근 M턴만 유지(sliding window)."""

    turn_index: int = 0
    chunk_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)


@dataclass
class SessionState:
    """범용 멀티턴 세션 상태(SessionMemory 교체). variant-agnostic 코어 + namespaced
    `variant_state`(키=variant_id). 영속 키=session_id.

    코어(모든 variant 공유): recent_turns/running_summary(대화), tracked_references/
    retrieval_history/topic_signature(누적 컨텍스트·게이트 신호), last_variant_id(전환
    감지). variant 고유 의미(spec_driven 의 governing_normative_class/route 등)는
    `variant_state[variant_id]` 에 격리해 코어가 variant 를 모르게 한다(D2 — 범용성).

    설계: docs/plans/spec_driven_session_memory.design.v1.md §1."""

    # --- 식별 ---
    session_id: str
    user_id: str | None = None
    project_id: str | None = None
    last_variant_id: str | None = None  # 직전 턴을 처리한 variant(전환 감지)
    # --- 대화 코어 ---
    turn_count: int = 0
    recent_turns: list[ChatTurn] = field(default_factory=list)  # 최근 N턴 원문
    running_summary: str = ""  # keep_turns 초과분 LLM 압축 누적
    # --- 누적 컨텍스트(게이트·anaphora·검색 scope 신호) ---
    tracked_references: list[TrackedReference] = field(default_factory=list)
    retrieval_history: list[RetrievalTrace] = field(default_factory=list)
    topic_signature: str | None = None  # 주제 식별 라벨/해시(전환 감지)
    # --- 메모리 사용 이력(재현) ---
    last_memory_ids_used: list[str] = field(default_factory=list)
    # --- variant 확장(namespaced; 키=variant_id) ---
    variant_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    # --- 수명 ---
    updated_at: datetime | None = None
    expires_at: datetime | None = None
