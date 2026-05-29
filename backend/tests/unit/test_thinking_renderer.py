"""ThinkingRenderer: AgentEvent → list[str] reasoning lines. Pure-function tests."""
from __future__ import annotations

from app.api.thinking_renderer import render
from app.application.agents.events import AgentEvent


def _step(name: str, status: str, **payload) -> AgentEvent:
    return AgentEvent(kind="step", name=name, status=status, payload=payload)


def _tool(name: str, status: str, **payload) -> AgentEvent:
    return AgentEvent(kind="tool", name=name, status=status, payload=payload)


def _r(event, **kw):
    return render(event, **kw)


def test_intent_classification_started_and_ok():
    lines = _r(_step("intent_classification", "started", query="APR1400 안전계통"))
    assert "Classifying" in lines[0]
    assert any("query" in line and "APR1400" in line for line in lines)

    lines = _r(
        _step(
            "intent_classification",
            "ok",
            scenario_object="A",
            scenario_depth="L1",
            confidence=0.87,
            entities={"reactor_model": ["APR1400"], "topic": ["safety"]},
        )
    )
    assert "scenario A" in lines[0] and "L1" in lines[0] and "0.87" in lines[0]
    assert any("reactor_model=APR1400" in line for line in lines)


def test_retrieval_verbose_preview_with_cap_and_more():
    chunks = [
        {
            "chunk_id": f"c{i}",
            "document_id": f"doc{i}",
            "title": f"Doc Title {i}",
            "page": 10 + i,
            "score": 0.9 - i * 0.05,
            "doc_type": "10CFR",
            "snippet": f"snippet text {i} " * 5,
        }
        for i in range(5)
    ]
    lines = _r(
        _step("retrieval", "ok", num_chunks=5, chunks_preview=chunks),
        content_mode="metadata",
        max_items=3,
    )
    body = "\n".join(lines)
    assert "Retrieved 5" in lines[0]
    assert "Doc Title 0" in body
    assert "Doc Title 2" in body
    assert "Doc Title 3" not in body  # capped at 3
    assert "… 2 more" in body
    assert "[10CFR]" in body
    # metadata mode → no snippet leaked.
    assert "snippet text" not in body


def test_retrieval_snippet_mode_includes_truncated_snippet():
    chunks = [
        {
            "chunk_id": "c0",
            "document_id": "doc0",
            "title": "T",
            "page": 1,
            "score": 0.9,
            "snippet": "x" * 500,
        }
    ]
    lines = _r(
        _step("retrieval", "ok", num_chunks=1, chunks_preview=chunks),
        content_mode="snippets",
        max_items=3,
    )
    body = "\n".join(lines)
    # Snippet present but truncated at 200 chars in snippets mode.
    assert "x" * 50 in body
    assert "x" * 300 not in body


def test_retrieval_zero_chunks():
    lines = _r(_step("retrieval", "ok", num_chunks=0, chunks_preview=[]))
    assert "No matching" in lines[0]


def test_retrieval_graceful_when_preview_missing():
    # Older variant payload (no chunks_preview) still produces the headline.
    lines = _r(_step("retrieval", "ok", num_chunks=4))
    assert lines and "Retrieved 4" in lines[0]


def test_session_memory_load_branches():
    inj = _r(
        _step(
            "session_memory_load",
            "ok",
            present=True,
            injected=True,
            reason="match",
            prior_scenario_object="A",
            prior_scenario_depth="L1",
            summary_preview="이전 대화 요약",
        )
    )
    body = "\n".join(inj)
    assert "injecting" in body
    assert "A" in body and "L1" in body
    assert "이전 대화 요약" in body

    skipped = _r(_step("session_memory_load", "ok", present=True, injected=False, reason="differs"))
    assert "skipping" in skipped[0]
    absent = _r(_step("session_memory_load", "ok", present=False, injected=False, reason="none"))
    assert "No prior" in absent[0]


def test_memory_approved_hits_preview():
    lines = _r(
        _step(
            "memory_approved_search",
            "ok",
            hit_count=4,
            hits_preview=[
                {"memory_id": f"mem-{i}", "score": 0.9 - i * 0.1} for i in range(4)
            ],
        ),
        max_items=2,
    )
    body = "\n".join(lines)
    assert "Matched 4" in lines[0]
    assert "mem-0" in body and "mem-1" in body
    assert "mem-2" not in body
    assert "… 2 more" in body


def test_citation_resolve_preview():
    lines = _r(
        _step(
            "citation_resolve",
            "ok",
            resolved_count=2,
            total=3,
            resolved_preview=[
                {"citation_id": "cite-0", "document_id": "10CFR50", "page": 12, "section": "55a"},
                {"citation_id": "cite-1", "document_id": "NUREG-0800", "page": 47, "section": None},
            ],
        )
    )
    body = "\n".join(lines)
    assert "Resolved 2 of 3" in lines[0]
    assert "cite-0" in body and "10CFR50" in body and "§55a" in body and "(p. 12)" in body


def test_verification_pass_partial_fail():
    p = _r(_step("verification", "ok", verification_status="PASS",
                 citation_completeness=1.0, faithfulness=0.92))
    assert "passed" in p[0] and "0.92" in p[0]
    part = _r(_step("verification", "ok", verification_status="PARTIAL",
                    citation_completeness=0.5, faithfulness=0.7))
    assert "partial" in part[0]
    fail = _r(_step("verification", "ok", verification_status="FAIL",
                    citation_completeness=0.0, faithfulness=0.1))
    assert "failed" in fail[0]


def test_tool_error_surfaced_success_dropped():
    assert _r(_tool("retriever.search", "ok", latency_ms=12)) == []
    lines = _r(_tool("retriever.search", "error", error_code="timeout"))
    assert "retriever.search" in lines[0] and "timeout" in lines[0]


def test_token_and_reasoning_events_ignored():
    assert _r(AgentEvent(kind="token", payload={"content": "x"})) == []
    assert _r(AgentEvent(kind="reasoning", payload={"content": "x"})) == []


def test_unknown_step_returns_empty():
    assert _r(_step("not_a_real_step", "ok")) == []
    assert _r(_step("session_memory_update", "ok")) == []


# --- v3.1 (hierarchical_corrective_v3_1) step vocabulary ------------------

_V3 = "hierarchical_corrective_v3_1"
_V2 = "sequential_tool_routed_v2"


def test_v3_query_understanding():
    lines = _r(_step("query_understanding", "ok", multi_intent=True, sub_questions=3),
               variant_id=_V3)
    assert "3 sub-question" in lines[0] and "multi-intent" in lines[0]


def test_v3_retrieval_plan_and_execute():
    plan = _r(_step("retrieval_plan", "ok", rule_id="R-7",
                    plan_hash="abc", strategies=["bm25", "vector"]),
              variant_id=_V3)
    assert "R-7" in plan[0] and "bm25" in plan[0] and "vector" in plan[0]

    ex = _r(_step("retrieval_execute", "ok", num_chunks=3, pool_size=20,
                  strategies_ok=["bm25"], strategies_failed=["vector"]),
            variant_id=_V3)
    assert "Retrieved 3 of 20" in ex[0] and "vector" in ex[0]


def test_v3_retrieval_evaluate_surfaces_gate_decision():
    lines = _r(_step("retrieval_evaluate", "ok", overall="WEAK",
                     regulatory_enforced=True, num_pass=2),
               variant_id=_V3)
    assert "WEAK" in lines[0] and "2 passage" in lines[0]
    assert "regulatory" in lines[0]


def test_v3_retrieval_recover_diagnostic_reasoning():
    started = _r(_step("retrieval_recover", "started", round=0,
                       diagnosis="low entity coverage", strategy="synonym_expand"),
                 variant_id=_V3)
    assert "low entity coverage" in started[0] and "synonym_expand" in started[0]
    ok = _r(_step("retrieval_recover", "ok", round=0, outcome="PASS"), variant_id=_V3)
    assert "round 0" in ok[0] and "PASS" in ok[0]
    skipped = _r(_step("retrieval_recover", "skipped"), variant_id=_V3)
    assert "no recovery" in skipped[0].lower()


def test_v3_evidence_snippet_and_memory_inject():
    snip = _r(_step("evidence_snippet", "ok", num_snippets=5, pack_hash="h"),
              variant_id=_V3)
    assert "5 evidence window" in snip[0]
    inj = _r(_step("memory_inject", "ok", inject=True, num_memory_refs=2), variant_id=_V3)
    assert "2 memory item" in inj[0]
    # Transparent no-op steps are intentionally silent (kept out of the trace).
    assert _r(_step("memory_inject", "ok", inject=False, num_memory_refs=0),
              variant_id=_V3) == []
    assert _r(_step("memory_inject", "started"), variant_id=_V3) == []


def test_v3_claim_decompose_and_verify():
    dec = _r(_step("claim_decompose", "ok", num_claims=4, method="llm"), variant_id=_V3)
    assert "4 claim" in dec[0] and "llm" in dec[0]
    ver = _r(_step("claim_verify", "ok", verification_status="PARTIAL",
                   num_claims=4, contradicted=True, entailment_ran=True),
             variant_id=_V3)
    assert "PARTIAL" in ver[0] and "4 claim" in ver[0]
    assert "contradicted" in ver[0] and "entailment" in ver[0]


def test_v3_skipped_and_transparent_nodes_are_silent():
    # No-op / skipped corrective steps add noise → not narrated.
    assert _r(_step("multi_hop_expand", "skipped"), variant_id=_V3) == []
    assert _r(_step("selective_regenerate", "skipped"), variant_id=_V3) == []
    assert _r(_step("scenario_routing", "ok"), variant_id=_V3) == []
    # …but when they actually do work, they are narrated.
    assert "Regenerated 2" in _r(
        _step("selective_regenerate", "ok", num_regenerated=2), variant_id=_V3)[0]
    assert "2 hop" in _r(
        _step("multi_hop_expand", "ok", num_hops=2), variant_id=_V3)[0]


def test_variant_dispatch_isolation():
    # v2-only step name is not narrated under the v3.1 table…
    assert _r(_step("retrieval", "ok", num_chunks=2), variant_id=_V3) == []
    assert _r(_step("verification", "ok", verification_status="PASS"), variant_id=_V3) == []
    # …and v3.1-only step names are not narrated under the v2 table.
    assert _r(_step("retrieval_evaluate", "ok", overall="PASS"), variant_id=_V2) == []
    assert _r(_step("claim_verify", "ok", verification_status="PASS"), variant_id=_V2) == []


def test_shared_steps_render_under_both_variants():
    payload = dict(scenario_object="A", scenario_depth="L1", confidence=0.9)
    v2 = _r(_step("intent_classification", "ok", **payload), variant_id=_V2)
    v3 = _r(_step("intent_classification", "ok", **payload), variant_id=_V3)
    assert v2 == v3 and "scenario A" in v2[0]


def test_default_union_when_variant_unknown():
    # No variant context → union table narrates both vocabularies.
    assert "Retrieved 4" in _r(_step("retrieval", "ok", num_chunks=4))[0]
    assert "WEAK" in _r(_step("retrieval_evaluate", "ok", overall="WEAK", num_pass=1))[0]
