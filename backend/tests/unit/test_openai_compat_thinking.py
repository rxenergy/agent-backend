"""Thinking surface in /v1/chat/completions:

The shipped variant (spec_driven_v1) routes its thinking surface through the
LLM nodes' own output — `reasoning` events — not deterministic step/tool
narration. The renderer therefore:
  - drops `step` events (no narration line),
  - passes `reasoning` events straight through to `delta.reasoning_content`
    (streaming) / the `<think>…</think>` prefix (non-streaming),
  - surfaces failed `tool` events.
- `thinking_expose=false` disables both paths byte-equivalent to legacy.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeEchoLLM
from app.api import openai_compat
from app.application.events.recorder import EventRecorder
from app.config.profiles import AppContainer
from app.config.settings import Settings
from app.domain.interaction import AgentResponse
from app.domain.agents import VariantSpec


class _StubRunner:
    """Minimal runner emitting a fixed event sequence so we can assert the
    SSE / non-streaming translation independently of the real workflow.

    Mirrors the spec_driven thinking model: deterministic `step` events carry
    no narration (they flow on the `smr_agent.event` sidechannel + OTel only),
    while the generation LLM's own chain-of-thought rides `reasoning` events to
    the thinking surface."""

    spec = VariantSpec(variant_id="spec_driven_v1", compatible_llms=("fake-echo",))

    async def run_stream(self, request):
        from app.application.agents.events import AgentEvent

        # step events are dropped by the renderer (sidechannel-only).
        yield AgentEvent(kind="step", name="define_spec", status="started")
        yield AgentEvent(kind="step", name="retrieval", status="ok",
                         payload={"num_chunks": 3})
        # generation-LLM native CoT — split across deltas to exercise buffering.
        yield AgentEvent(kind="reasoning", payload={"content": "Let me reason about "})
        yield AgentEvent(kind="reasoning", payload={"content": "the cited regulation."})
        yield AgentEvent(kind="token", payload={"content": "안녕하세요"})
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text="안녕하세요",
            citations=(),
            refusal_reason=None,
            verification_status="pass",
            scenario_object="n_a",
            scenario_depth="n_a",
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


def _app(thinking_expose: bool, *, runner=None, variant: str = "spec_driven_v1") -> FastAPI:
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


def test_streaming_passes_reasoning_content():
    client = TestClient(_app(thinking_expose=True))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "spec_driven_v1@fake-echo",
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
    # generation-LLM native chain-of-thought passed straight through.
    assert "Let me reason about the cited regulation." in joined
    # Sidechannel still present — own client renders step/tool events.
    sidechannel = [c for c in chunks if "smr_agent" in c and "event" in c.get("smr_agent", {})]
    assert sidechannel


def test_streaming_disabled_suppresses_step_tool_narration():
    """`thinking_expose=false` disables deterministic step/tool *narration*. The
    generation LLM's own `reasoning` events still pass through (they are the
    model's output, not workflow narration) — same as the legacy contract."""
    client = TestClient(_app(thinking_expose=False))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "spec_driven_v1@fake-echo",
            "stream": True,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    chunks = _sse_chunks(resp.text)
    reasoning = "".join(
        c["choices"][0]["delta"]["reasoning_content"]
        for c in chunks
        if "reasoning_content" in c["choices"][0].get("delta", {})
    )
    # Only the model CoT (reasoning events) survives; step/tool narration does
    # not. With spec_driven steps producing no narration anyway, the surface is
    # exactly the passed-through reasoning.
    assert reasoning == "Let me reason about the cited regulation."


def test_non_streaming_prepends_think_block():
    client = TestClient(_app(thinking_expose=True))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "spec_driven_v1@fake-echo",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert content.startswith("<think>")
    assert "</think>" in content
    # generation-LLM reasoning is included in the think block.
    assert "Let me reason about the cited regulation." in content
    # Final answer follows the think block.
    answer_after = content.split("</think>", 1)[1].strip()
    assert answer_after == "안녕하세요"


def test_non_streaming_no_think_when_disabled():
    client = TestClient(_app(thinking_expose=False))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "spec_driven_v1@fake-echo",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert "<think>" not in content
    assert content == "안녕하세요"
