from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeToolLLM
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.confidence_scope import ConfidenceScopeTool
from app.adapters.tools.retrieval_scope import RetrievalScopeTool
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.submit_response import SubmitResponseTool
from app.adapters.tools.terminology_canonicalize import TerminologyCanonicalizeTool
from app.adapters.tools.terminology_expand import TerminologyExpandTool
from app.application.agents.react_loop import (
    REACT_TOOL_SPECS,
    run_react,
    tools_schema_hash,
)
from app.application.retrieval.corpus_map import CorpusMap
from app.application.terminology.vocab import TerminologyVocab
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.ports.llm import LLMToolResult, ToolCall
from app.ports.tool import ToolExecutionContext

# react_minimal_v1 N1 — ReAct Retrieval 루프. 종료 = (submit_response | max_turns
# backstop). finder 와 달리 recover_limit 없음 — 모델이 스스로 종료를 판정한다.

_CTX = ToolExecutionContext(
    interaction_id="i", trace_id="", app_profile="local",
    agent_variant="react_minimal_v1",
)
_VOCAB = Path(__file__).resolve().parents[3] / "tools" / "terminology" / "vocab.yaml"


def _executor(tmp: Path) -> ToolExecutor:
    body = {"tools": {
        "confidence.scope": {"version": "v1", "adapter": "scope_coverage", "timeout_ms": 1000, "retry": 0, "required": False},
        "terminology.canonicalize": {"version": "v1", "adapter": "vocab", "timeout_ms": 1000, "retry": 0, "required": False},
        "terminology.expand": {"version": "v1", "adapter": "vocab", "timeout_ms": 1000, "retry": 0, "required": False},
        "retrieval.scope": {"version": "v1", "adapter": "corpus_map", "timeout_ms": 1000, "retry": 0, "required": False},
        "retrieval.search": {"version": "v1", "adapter": "reranked", "timeout_ms": 6000, "retry": 0, "required": False},
        "submit_response": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
    }}
    p = tmp / "tools.yaml"
    p.write_text(yaml.safe_dump(body))
    registry = ToolRegistry.from_yaml(p)
    sink = FilesystemEventSink(root=str(tmp / "ev"), prefix="t")
    vocab = TerminologyVocab.from_yaml(_VOCAB)
    tools = {
        "confidence.scope": ConfidenceScopeTool(corpus_map=CorpusMap.default(), vocab=vocab),
        "terminology.canonicalize": TerminologyCanonicalizeTool(vocab=vocab),
        "terminology.expand": TerminologyExpandTool(vocab=vocab),
        "retrieval.scope": RetrievalScopeTool(),
        "retrieval.search": RetrievalSearchTool(retriever=LocalRetrieverTool(), reranker=IdentityReranker()),
        "submit_response": SubmitResponseTool(),
    }
    return ToolExecutor(registry=registry, tools=tools, event_sink=sink)


def _r(*calls: ToolCall, text: str = "", stop: str = "tool_calls") -> LLMToolResult:
    return LLMToolResult(text=text, tool_calls=tuple(calls), stop_reason=stop,
                         token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id="fake-tool")


def _scope() -> ToolCall:
    return ToolCall("c-scope", "confidence.scope", {"query_text": "i-SMR ECCS", "terms": ["ECCS"]})


def _canon() -> ToolCall:
    return ToolCall("c-canon", "terminology.canonicalize", {"terms": ["ECCS"]})


def _search(q="i-SMR ECCS") -> ToolCall:
    return ToolCall("c-search", "retrieval.search", {"query_text": q, "top_k": 3})


def _finish(outcome="answer", reason="ok", missing=None) -> ToolCall:
    args = {"outcome": outcome, "reason": reason}
    if missing is not None:
        args["missing_info"] = missing
    return ToolCall("c-finish", "submit_response", args)


async def _run(llm, ex, *, max_turns=8):
    return await run_react(
        llm=llm, tool_executor=ex, ctx=_CTX,
        system_prompt_body="react retrieval instructions",
        retrieval_policy_hash="pol16",
        query_text="i-SMR ECCS 요건", record=lambda r: None,
        max_turns=max_turns,
    )


@pytest.mark.asyncio
async def test_happy_path_finishes_with_answer_and_accumulates_chunks() -> None:
    # confidence.scope → canonicalize → search → submit_response(answer).
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(_scope()),
            _r(_canon()),
            _r(_search()),
            _r(_finish(outcome="answer", reason="evidence found")),
        ])
        result = await _run(llm, _executor(Path(tmp)))
        assert result.outcome == "answer"
        assert len(result.chunks) >= 1            # 검색 chunk 누적.
        assert result.term_coverage is not None    # confidence.scope 가 coverage 산출.


@pytest.mark.asyncio
async def test_out_of_scope_finish_propagates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[_r(_finish(outcome="out_of_scope", reason="off-domain"))])
        result = await _run(llm, _executor(Path(tmp)))
        assert result.outcome == "out_of_scope"
        assert result.chunks == []                 # 검색 없이 종료 가능.


@pytest.mark.asyncio
async def test_clarification_finish_carries_missing_info() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(_finish(outcome="clarification", reason="ambiguous", missing=["reactor name"]))
        ])
        result = await _run(llm, _executor(Path(tmp)))
        assert result.outcome == "clarification"
        assert result.missing_info == ("reactor name",)


@pytest.mark.asyncio
async def test_no_submit_within_max_turns_synthesizes_insufficient() -> None:
    # 매 턴 검색만, submit_response 없음 → max_turns backstop → 합성 insufficient.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[_r(_search())])  # 소진 시 반복.
        result = await _run(llm, _executor(Path(tmp)), max_turns=4)
        assert result.outcome == "insufficient_evidence"
        assert result.turns_used == 4
        assert "max_turns" in result.reason


@pytest.mark.asyncio
async def test_empty_tool_calls_hits_backstop_with_no_chunks() -> None:
    # 모델이 도구를 안 부른다 → max_turns backstop. 검색 없으니 chunks 비어 있다.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[_r(stop="stop")])  # 빈 tool_calls.
        result = await _run(llm, _executor(Path(tmp)), max_turns=3)
        assert result.turns_used == 3
        assert result.outcome == "insufficient_evidence"
        assert result.chunks == []


@pytest.mark.asyncio
async def test_search_failure_is_fed_back_not_raised() -> None:
    # retrieval.search 실패(query_text 누락)는 예외로 루프를 죽이지 않고 is_error 로
    # 되먹여진다 — 다음 턴에서 submit_response 로 종료.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c-bad", "retrieval.search", {})),  # query_text 누락.
            _r(_finish(outcome="insufficient_evidence", reason="search failed",
                       missing=["requirement_text"])),
        ])
        result = await _run(llm, _executor(Path(tmp)))
        assert result.outcome == "insufficient_evidence"
        assert result.chunks == []


@pytest.mark.asyncio
async def test_tool_calls_routed_through_executor_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        recorded: list[str] = []
        llm = FakeToolLLM(script=[_r(_scope()), _r(_search()), _r(_finish())])
        await run_react(
            llm=llm, tool_executor=_executor(Path(tmp)), ctx=_CTX,
            system_prompt_body="x", retrieval_policy_hash="p",
            query_text="q", record=lambda r: recorded.append(r.tool_name),
            max_turns=8,
        )
        assert recorded == ["confidence.scope", "retrieval.search", "submit_response"]


def test_tools_schema_hash_is_stable_and_covers_react_tools() -> None:
    assert {t.name for t in REACT_TOOL_SPECS} == {
        "confidence.scope", "terminology.canonicalize", "terminology.expand",
        "retrieval.scope", "retrieval.search", "submit_response"}
    assert tools_schema_hash() == tools_schema_hash()  # 결정론(재현 핀).
    assert len(tools_schema_hash()) == 16
