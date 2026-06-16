"""SSE end-to-end: POST /v1/chat/completions with stream=true must emit
OpenAI chat.completion.chunk frames — opening role chunk, content/usage
deltas, a terminal frame with finish_reason + smr_agent metadata, then
[DONE].
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeEchoLLM
from app.api import openai_compat
from app.application.agents.events import AgentEvent
from app.application.events.recorder import EventRecorder
from app.config.profiles import AppContainer
from app.config.settings import Settings
from app.domain.agents import VariantSpec
from app.domain.errors import VerificationStatus
from app.domain.interaction import AgentRequest, AgentResponse, Citation
from app.observability.otel import get_tracer

_TRACER = get_tracer("agent")


_FAKE_SPEC = VariantSpec(variant_id="fake_echo_v0", compatible_llms=("fake-echo",))


class _EchoStubRunner:
    """Local test double (was fake_echo_v0.FakeEchoAgentRunner) — single span, no tools."""

    def __init__(self, recorder: EventRecorder, spec: VariantSpec) -> None:
        self._recorder = recorder
        self.spec = spec

    async def run(self, request: AgentRequest) -> AgentResponse:
        started = time.monotonic()
        with _TRACER.start_as_current_span("agent.run") as span:
            span.set_attribute("agent.variant", self.spec.variant_id)
            span.set_attribute("interaction_id", request.interaction_id)
            answer = f"[echo] {request.query_text}"
            citations = (
                Citation(
                    citation_id="cite-0",
                    chunk_id="chunk-fake-0",
                    document_id="doc-fake",
                    page=1,
                    score=1.0,
                ),
            )
            response = AgentResponse(
                interaction_id=request.interaction_id,
                answer_text=answer,
                citations=citations,
                refusal_reason=None,
                verification_status=VerificationStatus.SKIPPED.value,
                scenario_object=None,
                scenario_depth=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                token_usage={
                    "prompt_tokens": len(request.query_text),
                    "completion_tokens": len(answer),
                },
            )
        event = self._recorder.build(
            request=request,
            response=response,
            agent_variant=self.spec.variant_id,
            started_at=started,
        )
        await self._recorder.persist(event)
        return response

    async def run_stream(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        response = await self.run(request)
        yield AgentEvent(kind="final", payload={"response": response}, ts=time.monotonic())


@pytest.fixture()
def fake_app():
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=str(Path(tmp) / "events"), prefix="t")
        recorder = EventRecorder(sink, app_profile="local")
        runners = {"fake_echo_v0": _EchoStubRunner(recorder=recorder, spec=_FAKE_SPEC)}
        llm_pool = {"fake-echo": FakeEchoLLM(model_id="fake-echo")}
        settings = Settings(
            agent_variants_enabled=["fake_echo_v0"],
            default_variant="fake_echo_v0",
            default_llm="fake-echo",
            utility_llm="fake-echo",
        )
        app = FastAPI()
        app.include_router(openai_compat.router)
        app.state.container = AppContainer(
            settings=settings, runners=runners, llm_pool=llm_pool, event_sink=sink,
        )
        yield app


def _parse_sse(body: str) -> list[dict | str]:
    out: list[dict | str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(payload))
    return out


def test_streaming_emits_chat_completion_chunks(fake_app):
    client = TestClient(fake_app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake_echo_v0@fake-echo",
            "stream": True,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(resp.text)
    # Must terminate with [DONE].
    assert frames[-1] == "[DONE]"

    chunks = [f for f in frames if isinstance(f, dict)]
    # Every chunk is OpenAI-shaped.
    for c in chunks:
        assert c["object"] == "chat.completion.chunk"
        assert c["model"].startswith("fake_echo_v0@")
        assert "choices" in c

    # Opening frame carries the assistant role.
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}

    # Exactly one terminal frame with finish_reason set.
    terminals = [c for c in chunks if c["choices"][0]["finish_reason"] is not None]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal["choices"][0]["finish_reason"] == "stop"
    assert "usage" in terminal
    # smr_agent metadata is OpenWebUI-invisible but our client uses it.
    assert "smr_agent" in terminal
    assert terminal["smr_agent"]["agent_variant"] == "fake_echo_v0"


def test_non_streaming_path_unchanged(fake_app):
    """Regression: stream=false must still return a single JSON object,
    not SSE."""
    client = TestClient(fake_app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake_echo_v0@fake-echo",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["smr_agent"]["agent_variant"] == "fake_echo_v0"
