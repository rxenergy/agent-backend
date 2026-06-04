from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

from app.domain.agents import Budget
from app.domain.interaction import (
    AgentRequest,
    AgentResponse,
    InteractionEvent,
    ToolCallRecord,
)
from app.domain.retrieval import (
    ChunkSignals,
    HopEdge,
    RecoverRound,
    SubQuestionDecision,
)
from app.domain.verification import ClaimVerification
from app.ports.event_sink import EventSinkPort

SCHEMA_VERSION = "interaction_event/v2"


def sha256_hex(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def current_trace_id() -> str:
    span = trace.get_current_span()
    if span is None:
        return ""
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return ""
    return format(ctx.trace_id, "032x")


class EventRecorder:
    def __init__(self, sink: EventSinkPort, *, app_profile: str) -> None:
        self._sink = sink
        self._app_profile = app_profile

    def build(
        self,
        *,
        request: AgentRequest,
        response: AgentResponse,
        agent_variant: str,
        retrieved_chunk_ids: tuple[str, ...] = (),
        retrieval_confidence: float = 0.0,
        prompt_profile_id: str | None = None,
        prompt_version: str | None = None,
        rendered_prompt_hash: str | None = None,
        prompt_composition_hash: str | None = None,
        prompt_fragment_versions: dict[str, str] | None = None,
        prompt_source: str | None = None,
        context_hash: str | None = None,
        classification_confidence: float = 0.0,
        classifier_policy_hash: str | None = None,
        classifier_intent: str | None = None,
        scope_tier: str | None = None,
        citation_completeness: float = 0.0,
        faithfulness: float = 0.0,
        started_at: float | None = None,
        error_code: str | None = None,
        tool_calls: tuple[ToolCallRecord, ...] = (),
        memory_ids_used: tuple[str, ...] = (),
        memory_types_used: tuple[str, ...] = (),
        memory_retrieval_scores: dict[str, float] | None = None,
        memory_review_statuses: dict[str, str] | None = None,
        memory_staleness_statuses: dict[str, str] | None = None,
        # --- v3.1 (hierarchical_corrective) reproducibility (default empty;
        # v2 callers omit these and get an unchanged event) ---
        query_understanding: dict[str, Any] | None = None,
        retrieval_plan_hash: str | None = None,
        corpus_map_hash: str | None = None,
        scope_mode: str | None = None,
        evaluator_policy_hash: str | None = None,
        regulatory_enforced: bool | None = None,
        per_chunk_signals: tuple[ChunkSignals, ...] = (),
        per_sub_question_decisions: tuple[SubQuestionDecision, ...] = (),
        recover_rounds: tuple[RecoverRound, ...] = (),
        section_merge_policy_hash: str | None = None,
        hops: tuple[HopEdge, ...] = (),
        evidence_pack_hash: str | None = None,
        claims: tuple[ClaimVerification, ...] = (),
        verifier_policy_hash: str | None = None,
        entailment_model: str | None = None,
        decompose_method: str | None = None,
        regulatory_grounding: str | None = None,
        budget: Budget | None = None,
    ) -> InteractionEvent:
        latency_ms = (
            int((time.monotonic() - started_at) * 1000)
            if started_at is not None
            else response.latency_ms
        )
        return InteractionEvent(
            schema_version=SCHEMA_VERSION,
            interaction_id=request.interaction_id,
            trace_id=current_trace_id(),
            timestamp=now_utc_iso(),
            app_profile=self._app_profile,
            agent_variant=agent_variant,
            model_id=request.model,
            query_text_hash=sha256_hex(request.query_text, 32),
            query_text_sample=request.query_text[:256],
            scenario_object=response.scenario_object,
            scenario_depth=response.scenario_depth,
            classification_confidence=classification_confidence,
            classifier_policy_hash=classifier_policy_hash,
            classifier_intent=classifier_intent,
            scope_tier=scope_tier,
            prompt_profile_id=prompt_profile_id,
            prompt_version=prompt_version,
            rendered_prompt_hash=rendered_prompt_hash,
            prompt_composition_hash=prompt_composition_hash,
            prompt_fragment_versions=dict(prompt_fragment_versions or {}),
            prompt_source=prompt_source,
            context_hash=context_hash,
            retrieval_doc_count=len(retrieved_chunk_ids),
            retrieved_chunk_ids=retrieved_chunk_ids,
            retrieval_confidence=retrieval_confidence,
            tool_calls=tool_calls,
            memory_ids_used=memory_ids_used,
            memory_types_used=memory_types_used,
            memory_retrieval_scores=memory_retrieval_scores or {},
            memory_review_statuses=memory_review_statuses or {},
            memory_staleness_statuses=memory_staleness_statuses or {},
            answer_hash=sha256_hex(response.answer_text, 32),
            citation_ids=tuple(c.citation_id for c in response.citations),
            verification_status=response.verification_status,
            citation_completeness=citation_completeness,
            faithfulness=faithfulness,
            latency_ms=latency_ms,
            token_usage=dict(response.token_usage),
            refusal_reason=response.refusal_reason,
            error_code=error_code,
            query_understanding=query_understanding,
            retrieval_plan_hash=retrieval_plan_hash,
            corpus_map_hash=corpus_map_hash,
            scope_mode=scope_mode,
            evaluator_policy_hash=evaluator_policy_hash,
            regulatory_enforced=regulatory_enforced,
            per_chunk_signals=per_chunk_signals,
            per_sub_question_decisions=per_sub_question_decisions,
            recover_rounds=recover_rounds,
            section_merge_policy_hash=section_merge_policy_hash,
            hops=hops,
            evidence_pack_hash=evidence_pack_hash,
            claims=claims,
            verifier_policy_hash=verifier_policy_hash,
            entailment_model=entailment_model,
            decompose_method=decompose_method,
            regulatory_grounding=regulatory_grounding,
            budget=budget,
        )

    async def persist(self, event: InteractionEvent) -> None:
        await self._sink.write_interaction_event(event)
