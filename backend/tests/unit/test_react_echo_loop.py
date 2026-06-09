from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeToolLLM
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.submit_response import SubmitResponseTool
from app.application.agents.react_loop import (
    REACT_ECHO_TOOL_SPECS,
    REACT_TOOL_SPECS,
    run_react,
    tools_schema_hash,
)
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.ports.llm import LLMToolResult, ToolCall
from app.ports.tool import ToolExecutionContext

# react_echo_v1 N1 — 도구-최소 ReAct 루프. run_react(tool_specs=REACT_ECHO_TOOL_SPECS).
# react_minimal 과 동일 mechanics(parameterized loop)지만 노출 도구는 2개뿐이다 —
# 검색 질의 작성은 전적으로 모델 추론(키워드 보존)이 한다.

_CTX = ToolExecutionContext(
    interaction_id="i", trace_id="", app_profile="local",
    agent_variant="react_echo_v1",
)


def _executor(tmp: Path) -> ToolExecutor:
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked", "timeout_ms": 6000, "retry": 0, "required": False},
        "submit_response": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
    }}
    p = tmp / "tools.yaml"
    p.write_text(yaml.safe_dump(body))
    registry = ToolRegistry.from_yaml(p)
    sink = FilesystemEventSink(root=str(tmp / "ev"), prefix="t")
    tools = {
        "retrieval.search": RetrievalSearchTool(retriever=LocalRetrieverTool(), reranker=IdentityReranker()),
        "submit_response": SubmitResponseTool(),
    }
    return ToolExecutor(registry=registry, tools=tools, event_sink=sink)


def _r(*calls: ToolCall, text: str = "", stop: str = "tool_calls") -> LLMToolResult:
    return LLMToolResult(text=text, tool_calls=tuple(calls), stop_reason=stop,
                         token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id="fake-tool")


def _search(q="i-SMR ECCS single failure criterion") -> ToolCall:
    return ToolCall("c-search", "retrieval.search", {"query_text": q, "top_k": 3})


def _finish(outcome="answer", reason="ok", missing=None) -> ToolCall:
    args = {"outcome": outcome, "reason": reason}
    if missing is not None:
        args["missing_info"] = missing
    return ToolCall("c-finish", "submit_response", args)


async def _run(llm, ex, *, max_turns=8):
    return await run_react(
        llm=llm, tool_executor=ex, ctx=_CTX,
        system_prompt_body="react echo retrieval instructions",
        retrieval_policy_hash="echo16",
        query_text="i-SMR ECCS 단일고장기준", record=lambda r: None,
        max_turns=max_turns,
        tool_specs=REACT_ECHO_TOOL_SPECS,
    )


def test_echo_tool_set_is_exactly_search_and_submit() -> None:
    # 최소-도구 불변식 — confidence.scope·terminology.*·retrieval.scope 제거.
    assert {t.name for t in REACT_ECHO_TOOL_SPECS} == {
        "retrieval.search", "submit_response"}


def test_echo_schema_hash_differs_from_minimal() -> None:
    # 두 variant 의 tools_schema_hash 가 구별 → InteractionEvent 재현 핀 구별(원칙 5).
    assert tools_schema_hash(REACT_ECHO_TOOL_SPECS) != tools_schema_hash(REACT_TOOL_SPECS)
    assert len(tools_schema_hash(REACT_ECHO_TOOL_SPECS)) == 16


def test_minimal_tool_set_unchanged_by_extraction() -> None:
    # _SUBMIT_RESPONSE_SPEC 추출이 REACT_TOOL_SPECS 를 깨지 않는지(이름 세트 핀).
    assert {t.name for t in REACT_TOOL_SPECS} == {
        "confidence.scope", "terminology.canonicalize", "terminology.expand",
        "retrieval.scope", "retrieval.search", "submit_response"}


@pytest.mark.asyncio
async def test_search_then_answer_accumulates_chunks_no_scope_pins() -> None:
    # search → submit_response(answer). confidence.scope 부재 → term_coverage None.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(_search()),
            _r(_finish(outcome="answer", reason="evidence found")),
        ])
        result = await _run(llm, _executor(Path(tmp)))
        assert result.outcome == "answer"
        assert len(result.chunks) >= 1
        assert result.term_coverage is None      # scope 도구 없음 → 핀 미산출.
        assert result.scope_mode is None
        assert result.tools_schema_hash == tools_schema_hash(REACT_ECHO_TOOL_SPECS)


@pytest.mark.asyncio
async def test_out_of_scope_finish_without_search() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[_r(_finish(outcome="out_of_scope", reason="off-domain"))])
        result = await _run(llm, _executor(Path(tmp)))
        assert result.outcome == "out_of_scope"
        assert result.chunks == []


@pytest.mark.asyncio
async def test_no_submit_within_max_turns_synthesizes_insufficient() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[_r(_search())])  # 소진 시 반복, submit 없음.
        result = await _run(llm, _executor(Path(tmp)), max_turns=4)
        assert result.outcome == "insufficient_evidence"
        assert result.turns_used == 4
        assert "max_turns" in result.reason


@pytest.mark.asyncio
async def test_only_echo_tools_are_routed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        recorded: list[str] = []
        llm = FakeToolLLM(script=[_r(_search()), _r(_finish())])
        await run_react(
            llm=llm, tool_executor=_executor(Path(tmp)), ctx=_CTX,
            system_prompt_body="x", retrieval_policy_hash="p",
            query_text="q", record=lambda r: recorded.append(r.tool_name),
            max_turns=8, tool_specs=REACT_ECHO_TOOL_SPECS,
        )
        assert recorded == ["retrieval.search", "submit_response"]
