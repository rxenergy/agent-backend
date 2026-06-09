"""Answer-body composition in /v1/chat/completions (answer_renderer at the
boundary): inline [cite-N]→[n] rewrite, References section with ADAMS links,
caveat callouts — on both streaming and non-streaming paths, with the streaming
ordering invariant (trailer content precedes the finish frame)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeEchoLLM
from app.api import openai_compat
from app.config.profiles import AppContainer
from app.config.settings import Settings
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentResponse, Citation


def _citation(cid, document_id, formatted):
    return Citation(citation_id=cid, document_id=document_id, formatted=formatted)


class _CiteStubRunner:
    """Streams a body with [cite-N] markers split across deltas, then a final
    response carrying citations + regulatory_grounding='unverified'."""

    spec = VariantSpec(
        variant_id="hierarchical_corrective_v3_1", compatible_llms=("fake-echo",)
    )

    async def run_stream(self, request):
        from app.application.agents.events import AgentEvent

        # [cite-0] intentionally split across two token deltas.
        for tok in ["자연순환을 쓴다", "[cite-", "0]. 압력은 X[cite-1]."]:
            yield AgentEvent(kind="token", payload={"content": tok})
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text="자연순환을 쓴다[cite-0]. 압력은 X[cite-1].",
            citations=(
                _citation("cite-0", "ML18002A422",
                          "[cite-0] [ML18002A422, Section C.I.4, p. 12, Rev. 5]"),
                _citation("cite-1", "RG-1.206",
                          "[cite-1] [RG-1.206, Section 1.1, p. 3, Rev. 2]"),
            ),
            refusal_reason=None,
            verification_status="pass",
            scenario_object="O1",
            scenario_depth="D2",
            latency_ms=1,
            token_usage={"prompt_tokens": 1, "completion_tokens": 1},
            llm_id="fake-echo",
            model_id="fake-echo",
            regulatory_grounding="unverified",
        )
        yield AgentEvent(kind="final", payload={"response": response})

    async def run(self, request):
        async for ev in self.run_stream(request):
            if ev.kind == "final":
                return ev.payload["response"]
        raise RuntimeError("no final")


def _app() -> FastAPI:
    tmp = tempfile.mkdtemp()
    sink = FilesystemEventSink(root=str(Path(tmp) / "events"), prefix="t")
    variant = "hierarchical_corrective_v3_1"
    settings = Settings(
        agent_variants_enabled=[variant],
        default_variant=variant,
        default_llm="fake-echo",
        utility_llm="fake-echo",
        thinking_expose=False,  # 본문 합성만 따로 검증(thinking 노이즈 제거).
    )
    app = FastAPI()
    app.include_router(openai_compat.router)
    app.state.container = AppContainer(
        settings=settings,
        runners={variant: _CiteStubRunner()},
        llm_pool={"fake-echo": FakeEchoLLM(model_id="fake-echo")},
        event_sink=sink,
    )
    return app


def _sse_chunks(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:].strip()))
    return out


def test_non_streaming_composes_links_refs_callout():
    client = TestClient(_app())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "hierarchical_corrective_v3_1@fake-echo",
              "messages": [{"role": "user", "content": "q"}]},
    )
    content = resp.json()["choices"][0]["message"]["content"]
    # 인라인 [cite-N] → [n].
    assert "[1]" in content and "[2]" in content
    assert "[cite-0]" not in content and "[cite-1]" not in content
    # References + ADAMS 링크 + 평문 fallback.
    assert "**근거 (References)**" in content
    assert "https://www.nrc.gov/docs/ML1800/ML18002A422.pdf" in content
    assert "[2] RG-1.206, Section 1.1, p. 3, Rev. 2" in content
    # 규제 미검증 callout(boundary 합성).
    assert "**규제 근거 미검증**" in content


def test_streaming_rewrites_markers_and_appends_trailer_before_finish():
    client = TestClient(_app())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "hierarchical_corrective_v3_1@fake-echo", "stream": True,
              "messages": [{"role": "user", "content": "q"}]},
    )
    chunks = _sse_chunks(resp.text)
    # content 조각 이어붙이기.
    content = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    assert "자연순환을 쓴다[1]. 압력은 X[2]." in content   # 경계 가로지른 마커 치환
    assert "[cite-" not in content
    assert "**근거 (References)**" in content
    assert "https://www.nrc.gov/docs/ML1800/ML18002A422.pdf" in content
    assert "**규제 근거 미검증**" in content

    # 순서 불변식: 모든 content 프레임은 finish 프레임보다 *먼저*.
    finish_idx = next(
        i for i, c in enumerate(chunks)
        if c["choices"][0].get("finish_reason") is not None
    )
    last_content_idx = max(
        i for i, c in enumerate(chunks)
        if c["choices"][0]["delta"].get("content")
    )
    assert last_content_idx < finish_idx
