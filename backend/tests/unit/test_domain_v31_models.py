from __future__ import annotations

import json
from dataclasses import asdict

from app.domain.agents import Budget
from app.domain.errors import RefusalReason
from app.domain.interaction import AgentResponse, Citation, InteractionEvent
from app.domain.query import QueryPlan, SubQuestion
from app.domain.retrieval import (
    ChunkSignals,
    EvaluationResult,
    EvidencePack,
    EvidenceSnippet,
    GateDecision,
    HopEdge,
    RecoverRound,
    RetrievalPlan,
    RetrievalStrategy,
    SubQuestionDecision,
)
from app.domain.verification import (
    Claim,
    ClaimChecks,
    ClaimStatus,
    ClaimType,
    ClaimVerification,
)


# --- str-enum idiom: asdict-then-json must yield the value, not the repr ----


def test_new_enums_are_str_enums():
    # A plain Enum would serialize to "ClaimStatus.SUPPORTED" via default=str.
    assert ClaimStatus.SUPPORTED.value == "supported"
    assert GateDecision.WEAK.value == "weak"
    assert ClaimType.COMPARISON.value == "comparison"
    assert RefusalReason.INSUFFICIENT_EVIDENCE.value == "insufficient_evidence"
    assert RefusalReason.BUDGET_EXCEEDED.value == "budget_exceeded"
    # str-enum members are str instances → json serializes the value directly.
    assert json.dumps(ClaimStatus.SUPPORTED) == '"supported"'


# --- construction + defaults ------------------------------------------------


def test_query_plan_defaults():
    qp = QueryPlan()
    assert qp.sub_questions == ()
    assert qp.multi_intent is False
    assert qp.decompose_prompt_hash is None


def test_retrieval_plan_construction():
    plan = RetrievalPlan(
        rule_id="comparison_with_regulation_id",
        strategies=(RetrievalStrategy(name="search_hybrid"),),
        plan_hash="abc123",
    )
    assert plan.fusion == "rerank"  # v3.1 — RRF 제거, reranker 가 순위 권위.
    assert plan.strategies[0].name == "search_hybrid"


def test_evidence_pack_construction():
    pack = EvidencePack(
        snippets=(EvidenceSnippet(snippet_id="s1", chunk_id="c1", text="t"),),
        pack_hash="ph",
        snippet_extractor_version="v1",
    )
    assert pack.snippets[0].chunk_id == "c1"


def test_claim_verification_nested_checks_default():
    cv = ClaimVerification(claim_id="cl1", text="x", status=ClaimStatus.SUPPORTED.value)
    assert isinstance(cv.checks, ClaimChecks)
    assert cv.checks.citation_resolves is False
    assert cv.evidence_strip_ids == ()


# --- THE round-trip: asdict -> json -> dict, enums as values ----------------
# A construction-only test passes even if a field holds a pydantic model that
# asdict won't recurse. This exercises the actual persistence path
# (FilesystemEventSink does `json.dumps(asdict(event), default=str)`).


def _full_event() -> InteractionEvent:
    return InteractionEvent(
        schema_version="interaction_event/v2",
        interaction_id="iid-1",
        trace_id="t",
        timestamp="2026-05-29T00:00:00+00:00",
        app_profile="local",
        agent_variant="hierarchical_corrective_v3_1",
        model_id="gemma-4-it",
        query_text_hash="qh",
        query_text_sample="q",
        scenario_object="O2",
        scenario_depth="D2",
        classification_confidence=0.82,
        prompt_profile_id="o2_d2_v1",
        prompt_version="v1",
        rendered_prompt_hash="rph",
        prompt_composition_hash="pch",
        prompt_fragment_versions={"system": "v3"},
        prompt_source="local",
        context_hash="ch",
        retrieval_doc_count=2,
        retrieved_chunk_ids=("c1", "c2"),
        retrieval_confidence=0.9,
        tool_calls=(),
        memory_ids_used=(),
        memory_types_used=(),
        memory_retrieval_scores={},
        memory_review_statuses={},
        memory_staleness_statuses={},
        answer_hash="ah",
        citation_ids=("cite-1",),
        verification_status="pass",
        citation_completeness=1.0,
        faithfulness=0.9,
        latency_ms=4200,
        # v3.1 extensions populated
        query_understanding={"ner_dict_version": "v1", "sub_question_count": 2},
        retrieval_plan_hash="abc123",
        evaluator_policy_hash="pol1",
        per_chunk_signals=(
            ChunkSignals(
                chunk_id="c1",
                s_lex=0.7,
                s_reg=0.8,
                s_total=0.75,
                hard_gates_passed=True,
                decision=GateDecision.PASS.value,
            ),
        ),
        per_sub_question_decisions=(
            SubQuestionDecision(sub_question_id="sq1", decision=GateDecision.PASS.value, n_pass=2),
        ),
        recover_rounds=(
            RecoverRound(round_index=0, diagnosis="entity_coverage", recover_strategy_id="synonym_expand"),
        ),
        hops=(HopEdge(from_chunk_id="c1", ref_kind="parent_section", target_id="c9"),),
        evidence_pack_hash="ph",
        claims=(
            ClaimVerification(
                claim_id="cl1",
                text="i-SMR uses passive ECCS",
                status=ClaimStatus.SUPPORTED.value,
                cite_marker="cite-1",
                evidence_strip_ids=("s1",),
                checks=ClaimChecks(citation_resolves=True, entailment_score=0.93),
            ),
        ),
        verifier_policy_hash="vph",
        entailment_model="llm:gemma-4-it",
        budget=Budget(llm_calls_used=4, total_llm_call_budget=8),
    )


def test_interaction_event_round_trips_through_asdict_json():
    event = _full_event()
    loaded = json.loads(json.dumps(asdict(event), default=str))

    # Nested dataclasses became dicts (not repr strings).
    assert loaded["per_chunk_signals"][0]["chunk_id"] == "c1"
    # str-enum values, not "GateDecision.PASS".
    assert loaded["per_chunk_signals"][0]["decision"] == "pass"
    assert loaded["claims"][0]["status"] == "supported"
    # Doubly-nested dataclass (ClaimChecks inside ClaimVerification).
    assert loaded["claims"][0]["checks"]["citation_resolves"] is True
    assert loaded["claims"][0]["checks"]["entailment_score"] == 0.93
    assert loaded["recover_rounds"][0]["diagnosis"] == "entity_coverage"
    assert loaded["hops"][0]["ref_kind"] == "parent_section"
    assert loaded["budget"]["llm_calls_used"] == 4
    assert loaded["query_understanding"]["sub_question_count"] == 2


def test_v2_style_event_omits_v31_values():
    """An event built without v3.1 fields keeps them empty/None — the v2
    shape is preserved (backward compatibility)."""
    event = InteractionEvent(
        schema_version="interaction_event/v2",
        interaction_id="iid-2",
        trace_id="",
        timestamp="2026-05-29T00:00:00+00:00",
        app_profile="local",
        agent_variant="sequential_tool_routed_v2",
        model_id="fake-echo",
        query_text_hash="qh",
        query_text_sample="q",
        scenario_object="O1",
        scenario_depth="D1",
        classification_confidence=0.5,
        prompt_profile_id=None,
        prompt_version=None,
        rendered_prompt_hash=None,
        prompt_composition_hash=None,
        prompt_fragment_versions={},
        prompt_source=None,
        context_hash=None,
        retrieval_doc_count=0,
        retrieved_chunk_ids=(),
        retrieval_confidence=0.0,
        tool_calls=(),
        memory_ids_used=(),
        memory_types_used=(),
        memory_retrieval_scores={},
        memory_review_statuses={},
        memory_staleness_statuses={},
        answer_hash="ah",
        citation_ids=(),
        verification_status="pass",
        citation_completeness=0.0,
        faithfulness=0.0,
        latency_ms=10,
    )
    loaded = json.loads(json.dumps(asdict(event), default=str))
    assert loaded["per_chunk_signals"] == []
    assert loaded["claims"] == []
    assert loaded["budget"] is None
    assert loaded["query_understanding"] is None


def test_agent_response_round_trips_with_v31_fields():
    resp = AgentResponse(
        interaction_id="iid-3",
        answer_text="ans",
        citations=(Citation(citation_id="cite-1"),),
        refusal_reason=None,
        verification_status="pass",
        scenario_object="O2",
        scenario_depth="D2",
        latency_ms=100,
        claims=(
            ClaimVerification(claim_id="cl1", text="x", status=ClaimStatus.PARTIAL.value),
        ),
        evaluation=EvaluationResult(overall_decision=GateDecision.WEAK.value),
        recover_rounds=(),
        hops=(),
    )
    loaded = json.loads(json.dumps(asdict(resp), default=str))
    assert loaded["claims"][0]["status"] == "partial"
    assert loaded["evaluation"]["overall_decision"] == "weak"


def test_claim_default_type_is_other():
    c = Claim(id="cl1", text="x")
    assert c.claim_type == "other"
    assert c.cite_marker is None
