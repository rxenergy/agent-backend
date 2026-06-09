from __future__ import annotations

import json
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
from app.adapters.tools.verification_local import (
    LocalCitationCheckTool,
    LocalFaithfulnessCheckTool,
)
from app.application.agents.llm_router import LLMRouter
from app.application.agents.react_echo_v1 import REACT_ECHO_VARIANT_ID, ReactEchoRunner
from app.application.agents.react_loop import REACT_ECHO_TOOL_SPECS
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.react_source import (
    ReactGenerationPromptSource,
    ReactRetrievalPromptSource,
)
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest
from app.ports.llm import LLMToolResult, ToolCall

# react_echo_v1 — 도구-최소(echo) 변형 end-to-end(fake). VariantRegistry.build →
# factory → runner 의 실제 선택 경로를 탄다 — echo 전용 deps 배선(react_echo_retrieval
# _prompt_source)과 2-도구 루프가 상속한 generation/verification harness 와 맞물리는지
# 검증한다(subclass 회귀 가드 — 상속한 run() 이 echo 에서 조용히 깨지지 않게).

_SPEC = VariantSpec(variant_id=REACT_ECHO_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"


def _tool_registry_yaml(root: Path) -> Path:
    # echo 가 실제로 쓰는 도구만 — 루프 2개 + 생성 후 관측 검증 2개.
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked", "timeout_ms": 6000, "retry": 0, "required": False},
        "submit_response": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
        "verification.citation_check": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
        "verification.faithfulness_check": {"version": "v1", "adapter": "local", "timeout_ms": 3000, "retry": 0, "required": False},
    }}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _tools() -> dict:
    return {
        "retrieval.search": RetrievalSearchTool(retriever=LocalRetrieverTool(), reranker=IdentityReranker()),
        "submit_response": SubmitResponseTool(),
        "verification.citation_check": LocalCitationCheckTool(),
        "verification.faithfulness_check": LocalFaithfulnessCheckTool(),
    }


def _deps(tmp: Path, *, llm) -> AgentDeps:
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    executor = ToolExecutor(registry=registry, tools=_tools(), event_sink=sink)
    llm_router = LLMRouter(pool={"fake-tool": llm}, default_id="fake-tool")
    return AgentDeps(
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        llm_router=llm_router,
        tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        # echo 전용 retrieval source(키워드-보존 프롬프트) + 공유 generation source.
        react_echo_retrieval_prompt_source=ReactRetrievalPromptSource(
            _REPO_PROMPTS, profile_id="react_retrieval_echo_v1"
        ),
        react_generation_prompt_source=ReactGenerationPromptSource(_REPO_PROMPTS),
        tunables={
            "citation_contract_path": str(_CONTRACT),
            "react_max_turns": 8,
            "verification_citation_threshold": 0.9,
            "verification_faithfulness_threshold": 0.85,
        },
    )


def _r(*calls: ToolCall, text: str = "", stop: str = "tool_calls") -> LLMToolResult:
    return LLMToolResult(text=text, tool_calls=tuple(calls), stop_reason=stop,
                         token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id="fake-tool")


def _answer_script() -> FakeToolLLM:
    # 도구-최소 경로: search → submit_response(answer). scope/canon/expand 없음.
    return FakeToolLLM(script=[
        _r(ToolCall("c1", "retrieval.search",
                    {"query_text": "i-SMR ECCS single failure criterion", "top_k": 3})),
        _r(ToolCall("c2", "submit_response", {"outcome": "answer", "reason": "evidence found"})),
    ])


def _build(tmp: Path, llm):
    return VariantRegistry.build(REACT_ECHO_VARIANT_ID, _SPEC, _deps(tmp, llm=llm))


def _event(tmp: Path) -> dict:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def test_variant_is_registered_as_echo_runner() -> None:
    assert REACT_ECHO_VARIANT_ID in VariantRegistry.known()
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _answer_script())
        assert isinstance(runner, ReactEchoRunner)
        assert runner._tool_specs is REACT_ECHO_TOOL_SPECS


@pytest.mark.asyncio
async def test_answer_path_end_to_end_with_two_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _answer_script())
        resp = await runner.run(AgentRequest(
            interaction_id="i-echo-1", query_text="i-SMR ECCS 단일고장기준 요건",
            model="react_echo_v1@fake-tool",
        ))
        assert resp.refusal_reason is None
        assert resp.answer_text
        assert resp.citations  # 검색 chunk → citation candidates.
        # 재현 핀: echo retrieval policy_hash 가 event 에 실린다(variant 구별).
        ev = _event(Path(tmp))
        assert ev["agent_variant"] == REACT_ECHO_VARIANT_ID
        qu = ev.get("query_understanding") or {}
        assert "react_retrieval" in qu
        # scope 도구 부재 → term_coverage 핀은 None.
        assert qu["react_retrieval"].get("term_coverage") is None


@pytest.mark.asyncio
async def test_out_of_scope_finish_refuses_without_generation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c1", "submit_response",
                        {"outcome": "out_of_scope", "reason": "off-domain"})),
        ])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(AgentRequest(
            interaction_id="i-echo-2", query_text="오늘 서울 날씨 알려줘",
        ))
        assert resp.refusal_reason == "out_of_scope"
        assert resp.citations == ()


@pytest.mark.asyncio
async def test_answer_with_zero_chunks_forces_retrieval_no_result() -> None:
    # outcome=answer 인데 검색을 안 했다 → 근거 0 → 강제 거부(결정=코드).
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c1", "submit_response", {"outcome": "answer", "reason": "no search"})),
        ])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(AgentRequest(
            interaction_id="i-echo-3", query_text="i-SMR ECCS 요건",
        ))
        assert resp.refusal_reason == "retrieval_no_result"
