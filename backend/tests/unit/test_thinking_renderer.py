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


# --- v3.1 (hierarchical_corrective_v3_1) — summary tier (default) ----------
# The summary tier is Korean, outcome-conditioned, and drops internal mechanics.
# Design: docs/references/thinking_output_design.md.

_V3 = "hierarchical_corrective_v3_1"
_V2 = "sequential_tool_routed_v2"


def test_v3_intent_uses_korean_scenario_label():
    lines = _r(_step("intent_classification", "ok",
                     scenario_object="O1", scenario_depth="D2", confidence=0.87,
                     entities={"reactor_model": ["NuScale"]}),
               variant_id=_V3)
    assert "이해했습니다" in lines[0]
    assert "공급사·노형 설계" in lines[0] and "기술 상세" in lines[0]
    assert "reactor_model=NuScale" in lines[0]
    # 내부 게이트 verdict·식별자·confidence 숫자는 요약에 노출하지 않는다.
    assert "0.87" not in lines[0]


def test_v3_query_understanding_only_when_multi():
    multi = _r(_step("query_understanding", "ok", multi_intent=True, sub_questions=3),
               variant_id=_V3)
    assert "3개 하위 질의" in multi[0]
    # 단일 의도(거의 항상)는 기계 동작 → 무음.
    assert _r(_step("query_understanding", "ok", multi_intent=False, sub_questions=0),
              variant_id=_V3) == []


def test_v3_retrieval_execute_shows_document_refs():
    started = _r(_step("retrieval_execute", "started"), variant_id=_V3)
    assert "검색하는 중" in started[0]

    preview = [
        {"title": "RG 1.206", "section": "C.I.4", "document_id": "rg1206"},
        {"title": "KEPIC-ENB", "section": "3.2", "document_id": "kepic"},
    ]
    ok = _r(_step("retrieval_execute", "ok", num_chunks=2, pool_size=20,
                  chunks_preview=preview), variant_id=_V3)
    assert "근거 2건" in ok[0]
    assert "RG 1.206 §C.I.4" in ok[0] and "KEPIC-ENB §3.2" in ok[0]
    # 내부 식별자(전략명·fused·pool)는 노출하지 않는다.
    assert "fused" not in ok[0] and "pool" not in ok[0]


def test_v3_retrieval_execute_zero_and_missing_preview():
    assert "찾지 못" in _r(_step("retrieval_execute", "ok", num_chunks=0),
                          variant_id=_V3)[0]
    # preview 없는 payload 도 헤드라인은 낸다.
    ok = _r(_step("retrieval_execute", "ok", num_chunks=3), variant_id=_V3)
    assert "근거 3건" in ok[0]


def test_v3_retrieval_evaluate_pass_silent_weak_surfaced():
    # PASS 는 검색 줄에 흡수 → 무음.
    assert _r(_step("retrieval_evaluate", "ok", overall="PASS", num_pass=3),
              variant_id=_V3) == []
    # WEAK/FAIL 만 사유와 함께 노출. 게이트 토큰("WEAK")은 노출하지 않는다.
    weak = _r(_step("retrieval_evaluate", "ok", overall="WEAK", num_pass=1,
                    diagnosis_reason="질의의 핵심 용어와 근거 매칭이 약합니다."),
              variant_id=_V3)
    assert "부분적" in weak[0] and "매칭이 약합니다" in weak[0]
    assert "WEAK" not in weak[0]


def test_v3_retrieval_recover_started_only():
    started = _r(_step("retrieval_recover", "started", round=0,
                       diagnosis="entity_coverage_low", strategy="synonym_expand"),
                 variant_id=_V3)
    assert "동의어 확장" in started[0] and "시도 1" in started[0]
    # ok/skipped 는 후속 검색·평가 줄로 드러나므로 중복 억제.
    assert _r(_step("retrieval_recover", "ok", round=0, outcome="PASS"),
              variant_id=_V3) == []
    assert _r(_step("retrieval_recover", "skipped"), variant_id=_V3) == []


def test_v3_memory_inject_and_multi_hop():
    inj = _r(_step("memory_inject", "ok", inject=True, num_memory_refs=2), variant_id=_V3)
    assert "전문가 검토 답변 2건" in inj[0]
    assert _r(_step("memory_inject", "ok", inject=False, num_memory_refs=0),
              variant_id=_V3) == []
    hop = _r(_step("multi_hop_expand", "ok", num_hops=2), variant_id=_V3)
    assert "교차 참조된 조항을 2건" in hop[0]
    assert _r(_step("multi_hop_expand", "skipped"), variant_id=_V3) == []


def test_v3_claim_verify_outcome_only_no_duplicate_regulatory_note():
    ver = _r(_step("claim_verify", "ok", verification_status="PARTIAL",
                   num_claims=4, num_supported=3, contradicted=False,
                   entailment_ran=True), variant_id=_V3)
    assert "주장 4개 중 3개 근거 일치" in ver[0]
    # 규제 미검증 한계는 answer_text(durable)에 실리므로 thinking 에서 중복하지 않는다.
    assert not any("규제 차원 검증은 미수행" in line for line in ver)
    # 모순 발견 시 표기.
    contra = _r(_step("claim_verify", "ok", verification_status="FAIL",
                      num_claims=2, num_supported=0, contradicted=True),
                variant_id=_V3)
    assert "모순" in contra[0]
    # 검증 생략 → 본문에 없는 신뢰 신호이므로 thinking 에 1줄 남긴다.
    skipped = _r(_step("claim_verify", "skipped"), variant_id=_V3)
    assert "생략" in skipped[0]
    assert not any("규제 차원 검증은 미수행" in line for line in skipped)


def test_v3_refused_closes_trace():
    line = _r(_step("refused", "ok", reason="insufficient_evidence"), variant_id=_V3)[0]
    assert "거부" in line and "근거를 확보하지 못" in line
    # 알 수 없는 사유도 안전한 기본 문구.
    fallback = _r(_step("refused", "ok", reason="weird"), variant_id=_V3)[0]
    assert "답변을 제공하지 못" in fallback


def test_v3_dropped_internal_nodes_are_silent_in_summary():
    # 내부 기계 동작은 요약에서 드롭(사이드채널/span 전용).
    for name, payload in [
        ("retrieval_plan", dict(rule_id="R-7", strategies=["bm25"])),
        ("evidence_snippet", dict(num_snippets=5)),
        ("context_build", dict(context_hash="h")),
        ("prompt_render", dict(profile_id="p", profile_version="v")),
        ("claim_decompose", dict(num_claims=4, method="llm")),
        ("scenario_routing", {}),
        ("section_merge", dict(num_merged=2)),
    ]:
        assert _r(_step(name, "ok", **payload), variant_id=_V3) == [], name


def test_v3_generation_started_then_silent():
    assert "작성하는 중" in _r(_step("generation", "started", llm_id="x"),
                            variant_id=_V3)[0]
    assert _r(_step("generation", "ok", completion_tokens=10), variant_id=_V3) == []


# --- verbosity tiers -------------------------------------------------------


def test_detailed_tier_reproduces_legacy_english_narration():
    # detailed = 구 per-node 영어 서술(개발/디버그). 드롭됐던 내부 노드도 다시 나온다.
    plan = _r(_step("retrieval_plan", "ok", rule_id="R-7", strategies=["bm25", "vector"]),
              variant_id=_V3, verbosity="detailed")
    assert "R-7" in plan[0] and "bm25" in plan[0]
    ev = _r(_step("retrieval_evaluate", "ok", overall="WEAK", num_pass=2,
                  regulatory_enforced=True), variant_id=_V3, verbosity="detailed")
    assert "WEAK" in ev[0] and "2 passage" in ev[0]
    snip = _r(_step("evidence_snippet", "ok", num_snippets=5), variant_id=_V3,
              verbosity="detailed")
    assert "5 evidence window" in snip[0]


def test_off_tier_silences_all_workflow_narration():
    assert _r(_step("intent_classification", "ok", scenario_object="O1",
                    scenario_depth="D2"), variant_id=_V3, verbosity="off") == []
    assert _r(_tool("retriever.search", "error", error_code="timeout"),
              verbosity="off") == []


def test_variant_dispatch_isolation():
    # v2-only step name is not narrated under the v3.1 (summary) table…
    assert _r(_step("retrieval", "ok", num_chunks=2), variant_id=_V3) == []
    assert _r(_step("verification", "ok", verification_status="PASS"), variant_id=_V3) == []
    # …and v3.1-only step names are not narrated under the v2 table.
    assert _r(_step("retrieval_evaluate", "ok", overall="PASS"), variant_id=_V2) == []
    assert _r(_step("claim_verify", "ok", verification_status="PASS"), variant_id=_V2) == []
    # refused is summary-only for v3.1; v2 has no such step.
    assert _r(_step("refused", "ok", reason="x"), variant_id=_V2) == []


def test_shared_step_diverges_v2_english_v3_korean():
    payload = dict(scenario_object="O1", scenario_depth="D2", confidence=0.9)
    v2 = _r(_step("intent_classification", "ok", **payload), variant_id=_V2)
    v3 = _r(_step("intent_classification", "ok", **payload), variant_id=_V3)
    assert "scenario O1" in v2[0]          # v2 unchanged (English)
    assert "이해했습니다" in v3[0]            # v3 summary (Korean)
    assert v2 != v3


def test_default_union_uses_v3_summary_for_v3_steps():
    # No variant context → union table; v2 names English, v3 names summary.
    assert "Retrieved 4" in _r(_step("retrieval", "ok", num_chunks=4))[0]
    weak = _r(_step("retrieval_evaluate", "ok", overall="WEAK", num_pass=1,
                    diagnosis_reason="근거 매칭이 약합니다."))
    assert "부분적" in weak[0]
