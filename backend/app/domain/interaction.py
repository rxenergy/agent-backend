from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.agents import Budget
from app.domain.retrieval import (
    ChunkSignals,
    EvaluationResult,
    HopEdge,
    RecoverRound,
    SubQuestionDecision,
)
from app.domain.verification import ClaimVerification


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str


@dataclass(frozen=True)
class AgentRequest:
    interaction_id: str
    query_text: str
    chat_history: tuple[ChatTurn, ...] = ()
    model: str = "agent-search-v1"
    model_options: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class Citation:
    citation_id: str
    chunk_id: str | None = None
    document_id: str | None = None
    regulation_clause: str | None = None
    page: int | None = None
    score: float | None = None
    doc_type: str | None = None  # vendor | regulation | rai
    section: str | None = None
    revision: str | None = None
    response_date: str | None = None  # RAI 응답 일자
    formatted: str | None = None  # 기획 doc §4 Citation Format 적용 결과
    # 원문 다운로드 URL(인덱스 doc_metadata 1차 소스) — References 딥링크용. 없으면
    # answer_renderer 가 adams_url(ML번호 재구성) → 평문 순으로 강등한다.
    source_url: str | None = None


@dataclass(frozen=True)
class AgentResponse:
    interaction_id: str
    answer_text: str
    citations: tuple[Citation, ...]
    refusal_reason: str | None
    verification_status: str
    scenario_object: str | None
    scenario_depth: str | None
    latency_ms: int
    token_usage: dict[str, int] = field(default_factory=dict)
    classification_confidence: float = 0.0
    classifier_backend: str | None = None
    entities: dict[str, list[str]] = field(default_factory=dict)
    llm_id: str | None = None
    model_id: str | None = None
    # --- v3.1 (hierarchical_corrective) optional outputs ---
    # Default empty so v2 responses are unchanged. Surfaced to the client as
    # OpenAI-compat custom fields (the standard chat body is untouched).
    claims: tuple[ClaimVerification, ...] = ()
    evaluation: EvaluationResult | None = None
    recover_rounds: tuple[RecoverRound, ...] = ()
    hops: tuple[HopEdge, ...] = ()
    # 규제 근거 검증 축 — `verification_status`(claim 충실성)와 *직교*. v1 에선
    # clause_id/effective_on/authority 강제 부재라 "unverified". v2 enforce 시
    # "verified". 비규제 시나리오는 "n_a". 응답 객체·answer_text·custom field
    # 모두에 노출돼 v1 PASS 가 규제검증된 답으로 오인되지 않게 한다(PR-5 안전 계약).
    regulatory_grounding: str = "n_a"  # verified | unverified | n_a
    # Node 1 LLM 분류기의 open-world 신호(비-LLM backend 는 None). intent=답변
    # 내용 facet(taxonomy plan §5), scope_tier=T1–T4 처리 계층(§4). custom field
    # 로 클라이언트에 노출 + event 재현 단위.
    classifier_intent: str | None = None
    scope_tier: str | None = None


@dataclass(frozen=True)
class ToolCallRecord:
    """Compact tool invocation summary inlined into InteractionEvent."""

    name: str
    version: str
    status: str
    latency_ms: int
    input_hash: str
    output_hash: str | None = None
    error_code: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class InteractionEvent:
    """v2 §16 schema."""

    schema_version: str
    interaction_id: str
    trace_id: str
    timestamp: str

    app_profile: str
    agent_variant: str
    model_id: str

    query_text_hash: str
    query_text_sample: str | None

    scenario_object: str | None
    scenario_depth: str | None
    classification_confidence: float

    prompt_profile_id: str | None
    prompt_version: str | None
    rendered_prompt_hash: str | None
    prompt_composition_hash: str | None
    prompt_fragment_versions: dict[str, str]
    prompt_source: str | None

    context_hash: str | None
    retrieval_doc_count: int
    retrieved_chunk_ids: tuple[str, ...]
    retrieval_confidence: float

    tool_calls: tuple[ToolCallRecord, ...]

    memory_ids_used: tuple[str, ...]
    memory_types_used: tuple[str, ...]
    memory_retrieval_scores: dict[str, float]
    memory_review_statuses: dict[str, str]
    memory_staleness_statuses: dict[str, str]

    answer_hash: str
    citation_ids: tuple[str, ...]

    verification_status: str
    citation_completeness: float
    faithfulness: float

    latency_ms: int
    token_usage: dict[str, int] = field(default_factory=dict)

    refusal_reason: str | None = None
    error_code: str | None = None

    # --- v3.1 (hierarchical_corrective) reproducibility extensions ---
    # All default empty/None so v2 events are byte-identical except for the
    # absence of populated values. Embedded models are frozen dataclasses so
    # `dataclasses.asdict()` recurses cleanly (spec Appendix B).
    query_understanding: dict[str, Any] | None = None
    retrieval_plan_hash: str | None = None
    # Layer 1 범위 한정(corpus_map) 재현 단위. scope_mode∈{filter,boost,off}.
    # "scope 가 막다른 벽으로 좁혔나"는 RETRIEVAL_NO_RESULT/INSUFFICIENT_EVIDENCE
    # refusal 의 핵심 감사 질문이라 refusal 경로에도 실린다.
    corpus_map_hash: str | None = None
    scope_mode: str | None = None
    evaluator_policy_hash: str | None = None
    # 규제 hard gate 가 실제로 강제됐는지(v1=false). false 인 PASS 는 규제 근거가
    # *검증된* PASS 가 아님 — 감사/defensibility 가 이 둘을 구분해야 한다.
    regulatory_enforced: bool | None = None
    per_chunk_signals: tuple[ChunkSignals, ...] = ()
    per_sub_question_decisions: tuple[SubQuestionDecision, ...] = ()
    recover_rounds: tuple[RecoverRound, ...] = ()
    # v3.1 P1 Section auto-merge 정책 sha(승격·조립이 실행된 경우만). None=미실행.
    section_merge_policy_hash: str | None = None
    # P1b 예산 거버너(Node context_budget)가 수행한 강등·drop·재배치 액션 목록.
    # 빈 tuple=거버너 비활성(budget=0) 또는 액션 없음. 액션 상세는 metric 라벨이 아닌
    # event 로만 영속(고카디널리티 — chunk_id 포함). 인용 desync·근거 변형 감사용.
    context_budget_actions: tuple[str, ...] = ()
    hops: tuple[HopEdge, ...] = ()
    evidence_pack_hash: str | None = None
    claims: tuple[ClaimVerification, ...] = ()
    verifier_policy_hash: str | None = None
    entailment_model: str | None = None
    decompose_method: str | None = None  # "llm" | "fallback" — Node 14 가 실제 LLM 분해인지
    regulatory_grounding: str | None = None  # verified | unverified | n_a (response 와 동일 축)
    budget: Budget | None = None
    # Node 1 분류 정책 재현 핀(원칙 5) — rule 어휘/정규식/부스트, llm 프롬프트 등.
    classifier_policy_hash: str | None = None
    # Node 1 LLM 분류기 open-world 신호(원칙 5) — intent(12+unknown)·scope_tier
    # (T1–T4). 비-LLM backend·v2 경로는 None(이벤트 byte 영향 없음).
    classifier_intent: str | None = None
    scope_tier: str | None = None
