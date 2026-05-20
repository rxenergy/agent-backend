from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
