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
