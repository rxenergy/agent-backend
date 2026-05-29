"""Thinking surface in /v1/chat/completions:

- streaming: step/tool events produce `delta.reasoning_content` frames
  (OpenWebUI convention) in addition to the existing `smr_agent.event`
  sidechannel.
- non-streaming: thinking lines are prepended to `message.content` inside
  a `<think>…</think>` block.
- `thinking_expose=false` disables both paths byte-equivalent to legacy.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeEchoLLM
from app.api import openai_compat
from app.application.events.recorder import EventRecorder
from app.config.profiles import AppContainer
from app.config.settings import Settings
from app.domain.interaction import AgentRequest, AgentResponse
from app.domain.agents import VariantSpec


class _StubRunner:
    """Minimal runner emitting a fixed event sequence so we can assert the
    SSE / non-streaming translation independently of the real workflow."""

    spec = VariantSpec(variant_id="stub_v0", compatible_llms=("fake-echo",))

    async def run_stream(self, request):
        from app.application.agents.events import AgentEvent

        yield AgentEvent(kind="step", name="intent_classification", status="started")
        yield AgentEvent(
            kind="step",
            name="intent_classification",
            status="ok",
            payload={"scenario_object": "A", "scenario_depth": "L1", "confidence": 0.9},
        )
        yield AgentEvent(
            kind="step",
            name="retrieval",
            status="started",
            payload={"query": "APR1400 안전계통"},
        )
        yield AgentEvent(
            kind="step",
            name="retrieval",
            status="ok",
            payload={
                "num_chunks": 3,
                "chunks_preview": [
                    {
                        "chunk_id": "c0",
                        "document_id": "10CFR50",
                        "title": "10 CFR §50.55a",
                        "page": 12,
                        "score": 0.87,
                        "doc_type": "10CFR",
                    },
                    {
                        "chunk_id": "c1",
                        "document_id": "NUREG-0800",
                        "title": "SRP §3.9.3",
                        "page": 47,
                        "score": 0.81,
                        "doc_type": "SRP",
                    },
                    {
                        "chunk_id": "c2",
                        "document_id": "RG-1.26",
                        "title": "RG 1.26 Rev. 5",
                        "page": 8,
                        "score": 0.74,
                        "doc_type": "RG",
                    },
                ],
            },
        )
        yield AgentEvent(kind="token", payload={"content": "안녕하세요"})
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text="안녕하세요",
            citations=(),
            refusal_reason=None,
            verification_status="pass",
            scenario_object="A",
            scenario_depth="L1",
            latency_ms=1,
            token_usage={"prompt_tokens": 1, "completion_tokens": 1},
            classification_confidence=0.9,
            classifier_backend="rule",
            entities={},
            llm_id="fake-echo",
            model_id="fake-echo",
        )
        yield AgentEvent(kind="final", payload={"response": response})

    async def run(self, request):
        async for ev in self.run_stream(request):
            if ev.kind == "final":
                return ev.payload["response"]
        raise RuntimeError("no final")


class _V3StubRunner:
    """Stub emitting a v3.1 (hierarchical_corrective_v3_1) event sequence plus
    a `reasoning` event, so we can assert per-variant narration dispatch and
    that the generation LLM's native chain-of-thought reaches the thinking
    surface on both streaming and non-streaming paths."""

    spec = VariantSpec(
        variant_id="hierarchical_corrective_v3_1", compatible_llms=("fake-echo",)
    )

    async def run_stream(self, request):
        from app.application.agents.events import AgentEvent

        yield AgentEvent(kind="step", name="query_understanding", status="ok",
                         payload={"multi_intent": False, "sub_questions": 1})
        yield AgentEvent(kind="step", name="retrieval_evaluate", status="ok",
                         payload={"overall": "WEAK", "regulatory_enforced": True,
                                  "num_pass": 1})
        yield AgentEvent(kind="step", name="retrieval_recover", status="started",
                         payload={"round": 0, "diagnosis": "low entity coverage",
                                  "strategy": "synonym_expand"})
        yield AgentEvent(kind="step", name="retrieval_recover", status="ok",
                         payload={"round": 0, "outcome": "PASS"})
        yield AgentEvent(kind="step", name="generation", status="started",
                         payload={"llm_id": "fake-echo"})
        # Generation LLM native CoT — split across deltas to exercise buffering.
        yield AgentEvent(kind="reasoning", payload={"content": "Let me reason about "})
        yield AgentEvent(kind="reasoning", payload={"content": "the cited regulation."})
        yield AgentEvent(kind="token", payload={"content": "답변"})
        yield AgentEvent(kind="step", name="claim_verify", status="ok",
                         payload={"verification_status": "PARTIAL", "num_claims": 2,
                                  "contradicted": False, "entailment_ran": True})
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text="답변",
            citations=(),
            refusal_reason=None,
            verification_status="PARTIAL",
            scenario_object="A",
            scenario_depth="L1",
            latency_ms=1,
            token_usage={"prompt_tokens": 1, "completion_tokens": 1},
            classification_confidence=0.9,
            classifier_backend="rule",
            entities={},
            llm_id="fake-echo",
            model_id="fake-echo",
        )
        yield AgentEvent(kind="final", payload={"response": response})

    async def run(self, request):
        async for ev in self.run_stream(request):
            if ev.kind == "final":
                return ev.payload["response"]
        raise RuntimeError("no final")


def _app(thinking_expose: bool, *, runner=None, variant: str = "stub_v0") -> FastAPI:
    tmp = tempfile.mkdtemp()
    sink = FilesystemEventSink(root=str(Path(tmp) / "events"), prefix="t")
    EventRecorder(sink, app_profile="local")  # not used by stub
    runners = {variant: runner or _StubRunner()}
    llm_pool = {"fake-echo": FakeEchoLLM(model_id="fake-echo")}
    settings = Settings(
        agent_variants_enabled=[variant],
        default_variant=variant,
        default_llm="fake-echo",
        utility_llm="fake-echo",
        thinking_expose=thinking_expose,
    )
    app = FastAPI()
    app.include_router(openai_compat.router)
    app.state.container = AppContainer(
        settings=settings, runners=runners, llm_pool=llm_pool, event_sink=sink,
    )
    return app


def _app_v3(thinking_expose: bool) -> FastAPI:
    return _app(thinking_expose, runner=_V3StubRunner(),
                variant="hierarchical_corrective_v3_1")


def _sse_chunks(body: str) -> list[dict]:
    out: list[dict] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


def test_streaming_emits_reasoning_content_for_steps():
    client = TestClient(_app(thinking_expose=True))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "stub_v0@fake-echo",
            "stream": True,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    assert resp.status_code == 200
    chunks = _sse_chunks(resp.text)
    reasoning = [
        c["choices"][0]["delta"]["reasoning_content"]
        for c in chunks
        if "reasoning_content" in c["choices"][0].get("delta", {})
    ]
    assert reasoning, "no reasoning_content frames emitted"
    joined = "".join(reasoning)
    assert "Classifying" in joined
    assert "scenario A" in joined
    assert "Retrieved 3" in joined
    # Verbose preview: search query echo + at least one document title.
    assert "APR1400" in joined
    assert "10 CFR §50.55a" in joined
    assert "SRP §3.9.3" in joined
    # metadata mode (default) → no snippet text leaked even if payload had one.
    # Each reasoning frame is a single logical line ending with \n.
    for r in reasoning:
        assert r.endswith("\n")
    # Sidechannel still present — own client renders it.
    sidechannel = [c for c in chunks if "smr_agent" in c and "event" in c.get("smr_agent", {})]
    assert sidechannel


def test_streaming_disabled_when_thinking_expose_false():
    client = TestClient(_app(thinking_expose=False))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "stub_v0@fake-echo",
            "stream": True,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    chunks = _sse_chunks(resp.text)
    reasoning = [
        c for c in chunks
        if "reasoning_content" in c["choices"][0].get("delta", {})
    ]
    assert reasoning == []


def test_non_streaming_prepends_think_block():
    client = TestClient(_app(thinking_expose=True))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "stub_v0@fake-echo",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert content.startswith("<think>")
    assert "</think>" in content
    assert "Classifying" in content
    # Verbose preview leaked into <think>: query echo + doc title.
    assert "APR1400" in content
    assert "10 CFR §50.55a" in content
    # Final answer follows the think block.
    answer_after = content.split("</think>", 1)[1].strip()
    assert answer_after == "안녕하세요"


def test_non_streaming_no_think_when_disabled():
    client = TestClient(_app(thinking_expose=False))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "stub_v0@fake-echo",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert "<think>" not in content
    assert content == "안녕하세요"


# --- v3.1 variant dispatch + LLM reasoning surfacing ----------------------


def test_v3_streaming_narrates_v3_steps_and_passes_reasoning():
    client = TestClient(_app_v3(thinking_expose=True))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "hierarchical_corrective_v3_1@fake-echo",
            "stream": True,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    assert resp.status_code == 200
    chunks = _sse_chunks(resp.text)
    reasoning = [
        c["choices"][0]["delta"]["reasoning_content"]
        for c in chunks
        if "reasoning_content" in c["choices"][0].get("delta", {})
    ]
    joined = "".join(reasoning)
    # v3.1-specific narration (dispatched by runner.spec.variant_id).
    assert "Gate decision WEAK" in joined
    assert "low entity coverage" in joined
    assert "Verification PARTIAL" in joined
    # Generation LLM native chain-of-thought passed straight through.
    assert "Let me reason about the cited regulation." in joined


def test_v3_non_streaming_includes_model_reasoning_in_think_block():
    client = TestClient(_app_v3(thinking_expose=True))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "hierarchical_corrective_v3_1@fake-echo",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert content.startswith("<think>") and "</think>" in content
    think = content.split("</think>", 1)[0]
    # v3.1 step narration present…
    assert "Gate decision WEAK" in think
    assert "Verification PARTIAL" in think
    # …and the buffered generation-LLM reasoning is included as one block.
    assert "Let me reason about the cited regulation." in think
    # token body is not in <think>; it is the final answer.
    answer_after = content.split("</think>", 1)[1].strip()
    assert answer_after == "답변"
