"""spec_driven_v1 N1/N2 thinking — native-preferred · rationale-backstop hybrid.

Design: docs/plans/spec_driven_thinking_output.design.v1.md (D2/D3). The intake
nodes stream when an emitter is active: native reasoning (reasoning models) flows
to `reasoning` events under a lazy phase header; when native is absent (small /
Gemma onprem), the structured `reasoning` field is emitted as a backstop — one
source per node, no duplication. Emitter inactive → current non-stream path.
"""
from __future__ import annotations

import json

from app.adapters.llm.fake import FakeReasoningLLM
from app.application.agents.events import (
    EventEmitter,
    bind_emitter,
    unbind_emitter,
)
from app.application.intake.spec_driven_answer_spec import (
    SpecDrivenAnswerSpecInstantiator,
)
from app.application.intake.spec_driven_query import QueryFormulator
from app.domain.spec_driven import AnswerSpec, SpecSlot

_SPEC_JSON = json.dumps({
    "reasoning": "FIELD-RATIONALE",
    "intent": "requirement",
    "explicit_references": [],
    "required_slots": [{"name": "governing_clause", "keywords": ["10 CFR 50.46"]}],
})
_QUERIES_JSON = json.dumps({
    "reasoning": "FIELD-RATIONALE",
    "queries": [{"slot_name": "governing_clause",
                 "query_text": "10 CFR 50.46 ECCS"}],
})


async def _collect_reasoning(thunk) -> tuple[object, list[str]]:
    """Bind an active emitter, run `thunk()`, return (result, reasoning_texts)."""
    em = EventEmitter(active=True)
    token = bind_emitter(em)
    try:
        result = await thunk()
    finally:
        unbind_emitter(token)
    await em.close()
    texts = [ev.payload.get("content", "")
             async for ev in em.drain() if ev.kind == "reasoning"]
    return result, texts


def _n1(llm) -> SpecDrivenAnswerSpecInstantiator:
    return SpecDrivenAnswerSpecInstantiator(llm, prompt_body="Q: {query}", schema=None)


def _n2(llm) -> QueryFormulator:
    return QueryFormulator(llm, prompt_body="Q: {query} S: {spec}", schema=None)


def _spec() -> AnswerSpec:
    return AnswerSpec(
        intent="requirement",
        required_slots=(SpecSlot(name="governing_clause", keywords=("10 CFR 50.46",)),),
    )


# --- N1 Define Spec ---------------------------------------------------------


async def test_n1_native_cot_emitted_under_header_backstop_suppressed():
    llm = FakeReasoningLLM(content=_SPEC_JSON, reasoning="NATIVE-COT")
    spec, texts = await _collect_reasoning(
        lambda: _n1(llm).instantiate("질의", reasoning_label="답변 사양 정의")
    )
    joined = "".join(texts)
    assert "**답변 사양 정의**" in joined       # lazy header fired once
    assert "NATIVE-COT" in joined               # native CoT surfaced
    assert "FIELD-RATIONALE" not in joined      # backstop suppressed (1 source)
    assert spec.instantiation_method == "llm"   # parsing intact
    assert spec.required_slots[0].name == "governing_clause"


async def test_n1_rationale_backstop_when_no_native():
    llm = FakeReasoningLLM(content=_SPEC_JSON, reasoning="")  # non-reasoning model
    spec, texts = await _collect_reasoning(
        lambda: _n1(llm).instantiate("질의", reasoning_label="답변 사양 정의")
    )
    joined = "".join(texts)
    assert "**답변 사양 정의**" in joined       # header still fires (rationale present)
    assert "FIELD-RATIONALE" in joined          # structured field emitted as backstop
    assert spec.instantiation_method == "llm"


async def test_n1_inactive_emitter_uses_nonstream_path():
    # No emitter bound → NOOP → non-stream generate; spec still parses, no crash.
    llm = FakeReasoningLLM(content=_SPEC_JSON, reasoning="NATIVE-COT")
    spec = await _n1(llm).instantiate("질의")
    assert spec.instantiation_method == "llm"
    assert spec.required_slots[0].name == "governing_clause"


# --- N2 Query Formulation ---------------------------------------------------


async def test_n2_native_cot_emitted_backstop_suppressed():
    llm = FakeReasoningLLM(content=_QUERIES_JSON, reasoning="NATIVE-COT")
    (queries, method), texts = await _collect_reasoning(
        lambda: _n2(llm).formulate("질의", _spec(), reasoning_label="검색 쿼리 생성")
    )
    joined = "".join(texts)
    assert "**검색 쿼리 생성**" in joined
    assert "NATIVE-COT" in joined
    assert "FIELD-RATIONALE" not in joined
    assert method == "llm"
    assert queries[0].query_text.startswith("10 CFR 50.46")


async def test_n2_rationale_backstop_when_no_native():
    llm = FakeReasoningLLM(content=_QUERIES_JSON, reasoning="")
    (queries, method), texts = await _collect_reasoning(
        lambda: _n2(llm).formulate("질의", _spec(), reasoning_label="검색 쿼리 생성")
    )
    joined = "".join(texts)
    assert "**검색 쿼리 생성**" in joined
    assert "FIELD-RATIONALE" in joined
    assert method == "llm"


async def test_n2_no_header_when_no_reasoning_at_all():
    # Neither native CoT nor a `reasoning` field → no thinking, no empty header.
    content = json.dumps({"queries": [{"slot_name": "s", "query_text": "kw"}]})
    llm = FakeReasoningLLM(content=content, reasoning="")
    (queries, method), texts = await _collect_reasoning(
        lambda: _n2(llm).formulate("질의", _spec(), reasoning_label="검색 쿼리 생성")
    )
    assert texts == []          # lazy: no reasoning → no header
    assert method == "llm"
