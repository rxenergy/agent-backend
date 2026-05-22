"""Runner-level streaming: `run_stream()` must yield step / tool / token
events as the 15-step workflow advances and a terminal `final` event
carrying the same `AgentResponse` that the non-streaming `run()` produces.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.domain.interaction import AgentRequest
from tests.unit.test_sequential_tool_routed import _make_runner


@pytest.mark.asyncio
async def test_run_stream_yields_step_tool_token_and_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _ = _make_runner(Path(tmp))
        req = AgentRequest(
            interaction_id="i-stream",
            query_text="APR1400 안전계통",
            session_id="s-stream",
        )

        kinds: list[str] = []
        names: list[str] = []
        final_response = None
        async for ev in runner.run_stream(req):
            kinds.append(ev.kind)
            if ev.name:
                names.append(ev.name)
            if ev.kind == "final":
                final_response = ev.payload["response"]

        assert final_response is not None
        assert final_response.verification_status == "pass"
        # The terminal event must be `final` — nothing yields after it.
        assert kinds[-1] == "final"
        # Sanity: at least one step + one tool + one token event present.
        assert "step" in kinds
        assert "tool" in kinds
        assert "token" in kinds
        # Node-level coverage: intent_classification + verification both ran.
        assert "intent_classification" in names
        assert "verification" in names


@pytest.mark.asyncio
async def test_run_stream_matches_run_response() -> None:
    """`run_stream()`'s final event must carry an AgentResponse equivalent
    to what `run()` returns for the same request (same scenario,
    verification_status, citations count)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _ = _make_runner(Path(tmp))
        req1 = AgentRequest(interaction_id="i-a", query_text="질문", session_id="s")
        resp_blocking = await runner.run(req1)

    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _ = _make_runner(Path(tmp))
        req2 = AgentRequest(interaction_id="i-b", query_text="질문", session_id="s")
        resp_stream = None
        async for ev in runner.run_stream(req2):
            if ev.kind == "final":
                resp_stream = ev.payload["response"]

        assert resp_stream is not None
        assert resp_stream.verification_status == resp_blocking.verification_status
        assert len(resp_stream.citations) == len(resp_blocking.citations)
        assert resp_stream.answer_text == resp_blocking.answer_text
