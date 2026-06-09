from __future__ import annotations

import json
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
from app.adapters.tools.verification_local import (
    LocalCitationCheckTool,
    LocalFaithfulnessCheckTool,
)
from app.application.agents.llm_router import LLMRouter
from app.application.agents.react_minimal_v1 import REACT_MINIMAL_VARIANT_ID
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.react_source import (
    ReactGenerationPromptSource,
    ReactRetrievalPromptSource,
)
from app.application.retrieval.corpus_map import CorpusMap
from app.application.terminology.vocab import TerminologyVocab
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest
from app.ports.llm import LLMToolResult, LLMUnavailableError, ToolCall

# react_minimal_v1 — 최소 2-Phase ReAct conductor end-to-end(fake). VariantRegistry.
# build → factory → runner 의 실제 선택 경로를 탄다(deps 배선까지 검증).

_SPEC = VariantSpec(variant_id=REACT_MINIMAL_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"
_VOCAB = _REPO_PROMPTS.parent / "tools" / "terminology" / "vocab.yaml"


def _tool_registry_yaml(root: Path) -> Path:
    body = {"tools": {
        "confidence.scope": {"version": "v1", "adapter": "scope_coverage", "timeout_ms": 1000, "retry": 0, "required": False},
        "terminology.canonicalize": {"version": "v1", "adapter": "vocab", "timeout_ms": 1000, "retry": 0, "required": False},
        "terminology.expand": {"version": "v1", "adapter": "vocab", "timeout_ms": 1000, "retry": 0, "required": False},
        "retrieval.scope": {"version": "v1", "adapter": "corpus_map", "timeout_ms": 1000, "retry": 0, "required": False},
        "retrieval.search": {"version": "v1", "adapter": "reranked", "timeout_ms": 6000, "retry": 0, "required": False},
        "submit_response": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
        "verification.citation_check": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
        "verification.faithfulness_check": {"version": "v1", "adapter": "local", "timeout_ms": 3000, "retry": 0, "required": False},
    }}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _tools() -> dict:
    vocab = TerminologyVocab.from_yaml(_VOCAB)
    return {
        "confidence.scope": ConfidenceScopeTool(corpus_map=CorpusMap.default(), vocab=vocab),
        "terminology.canonicalize": TerminologyCanonicalizeTool(vocab=vocab),
        "terminology.expand": TerminologyExpandTool(vocab=vocab),
        "retrieval.scope": RetrievalScopeTool(),
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
        react_retrieval_prompt_source=ReactRetrievalPromptSource(_REPO_PROMPTS),
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
    return FakeToolLLM(script=[
        _r(ToolCall("c1", "confidence.scope", {"query_text": "i-SMR ECCS", "terms": ["ECCS"]})),
        _r(ToolCall("c2", "retrieval.search", {"query_text": "i-SMR ECCS", "top_k": 3})),
        _r(ToolCall("c3", "submit_response", {"outcome": "answer", "reason": "evidence found"})),
    ])


def _build(tmp: Path, llm):
    return VariantRegistry.build(REACT_MINIMAL_VARIANT_ID, _SPEC, _deps(tmp, llm=llm))


def _event(tmp: Path) -> dict:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def test_variant_is_registered() -> None:
    assert REACT_MINIMAL_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_answer_path_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _answer_script())
        req = AgentRequest(interaction_id="r1", query_text="i-SMR ECCS 요건")
        resp = await runner.run(req)
        assert resp.refusal_reason is None
        assert resp.scenario_object == "n_a" and resp.scenario_depth == "n_a"
        assert resp.regulatory_grounding == "n_a"
        assert len(resp.citations) >= 1            # 검색 chunk → 인용.

        rec = _event(Path(tmp))
        assert rec["agent_variant"] == REACT_MINIMAL_VARIANT_ID
        # 재현 핀 — 루프 산출은 query_understanding 백, 생성은 prompt 핀.
        react = rec["query_understanding"]["react_retrieval"]
        assert react["tools_schema_hash"] and react["policy_hash"]
        assert react["finish_outcome"] == "answer"
        assert rec["rendered_prompt_hash"]
        assert rec["prompt_profile_id"] == "react_generation_v1"
        # 생성 후 검증 도구가 invoke 되어 tool_calls 에 기록(관측 전용).
        names = {tc["name"] for tc in rec["tool_calls"]}
        assert {"verification.citation_check", "verification.faithfulness_check"} <= names
        assert {"confidence.scope", "retrieval.search", "submit_response"} <= names


@pytest.mark.asyncio
async def test_out_of_scope_finish_refuses_without_generation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c1", "submit_response", {"outcome": "out_of_scope", "reason": "off-domain"})),
        ])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(AgentRequest(interaction_id="r2", query_text="오늘 날씨"))
        assert resp.refusal_reason == "out_of_scope"
        assert resp.citations == ()
        rec = _event(Path(tmp))
        # 생성으로 안 들어갔으므로 검증 도구 미호출.
        assert "verification.citation_check" not in {tc["name"] for tc in rec["tool_calls"]}


@pytest.mark.asyncio
async def test_clarification_finish_refuses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c1", "submit_response",
                        {"outcome": "clarification", "reason": "ambiguous",
                         "missing_info": ["reactor name"]})),
        ])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(AgentRequest(interaction_id="r3", query_text="그거 요건 뭐야"))
        assert resp.refusal_reason == "clarification_required"


@pytest.mark.asyncio
async def test_answer_with_zero_chunks_forces_retrieval_no_result() -> None:
    # 모델이 검색 없이 answer 를 외쳐도 conductor 가 근거 0 을 보고 강제 거부한다
    # (결정=코드 — 근거 없이 생성 불가).
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c1", "submit_response", {"outcome": "answer", "reason": "I know it"})),
        ])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(AgentRequest(interaction_id="r4", query_text="ECCS 요건"))
        assert resp.refusal_reason == "retrieval_no_result"
        assert resp.citations == ()


class _UnavailableGenLLM(FakeToolLLM):
    """Retrieval 루프(generate_with_tools)는 정상, Generation(generate/stream)에서만
    LLMUnavailableError 를 던지는 fake — 생성 단계 가용성 장애 분기를 태운다."""

    async def generate(self, prompt, *, model_options=None, grammar=None):
        raise LLMUnavailableError("down")

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):
        raise LLMUnavailableError("down")
        yield  # pragma: no cover — generator 형태 유지.


@pytest.mark.asyncio
async def test_llm_unavailable_during_generation_refuses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = _UnavailableGenLLM(script=[
            _r(ToolCall("c1", "retrieval.search", {"query_text": "i-SMR ECCS", "top_k": 3})),
            _r(ToolCall("c2", "submit_response", {"outcome": "answer", "reason": "found"})),
        ])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(AgentRequest(interaction_id="r5", query_text="ECCS 요건"))
        assert resp.refusal_reason == "llm_unavailable"


@pytest.mark.asyncio
async def test_run_stream_emits_steps_tokens_then_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _answer_script())
        req = AgentRequest(interaction_id="r6", query_text="i-SMR ECCS 요건")
        events = [ev async for ev in runner.run_stream(req)]
        assert events[-1].kind == "final"
        step_names = {ev.name for ev in events if ev.kind == "step"}
        assert {"react_retrieval", "context_build", "generation"} <= step_names
        assert any(ev.kind == "token" for ev in events)   # 스트리밍 생성.


def test_generation_prompt_puts_language_rule_at_highest_recency() -> None:
    # 출력-언어 trailer 는 # QUERY *뒤*(최고 recency)에 와야 한다 — 영어 컨텍스트
    # 미러링 방지(plan: v4 trailer lesson). 본문의 언어 규칙만으로는 컨텍스트보다
    # 앞서므로 부족하다.
    from app.domain.retrieval import RetrievedChunk

    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _answer_script())
        cb = ContextBuilder(capture_mode="snippets")
        pack = cb.build(
            interaction_id="x", query_text="i-SMR ECCS 요건은?", chat_history=(),
            conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
            entities={}, chunks=[RetrievedChunk(
                chunk_id="c0", document_id="d0", score=1.0, snippet="ECCS text")],
        )
        text = runner._render_generation_prompt("i-SMR ECCS 요건은?", pack)
        assert "# RESPONSE LANGUAGE" in text
        # trailer 가 # QUERY 와 # CONTEXT 보다 뒤(최고 recency).
        assert text.index("# RESPONSE LANGUAGE") > text.index("# QUERY")
        assert text.index("# RESPONSE LANGUAGE") > text.index("# CONTEXT")


@pytest.mark.asyncio
async def test_retrieval_scope_mode_captured_as_event_pin() -> None:
    # retrieval.scope 호출 시 scope_mode 가 포착돼 이벤트 핀으로 실린다(corpus_map.
    # default → "off"). 죽은 필드 방지.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c1", "retrieval.scope", {})),
            _r(ToolCall("c2", "retrieval.search", {"query_text": "i-SMR ECCS", "top_k": 3})),
            _r(ToolCall("c3", "submit_response", {"outcome": "answer", "reason": "ok"})),
        ])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(AgentRequest(interaction_id="r8", query_text="ECCS 요건"))
        assert resp.refusal_reason is None
        rec = _event(Path(tmp))
        assert rec["scope_mode"] == "off"   # corpus_map.default → off(포착됨, None 아님).


@pytest.mark.asyncio
async def test_retrieval_source_not_wired_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _answer_script())
        runner._react_retrieval_source = None
        with pytest.raises(RuntimeError, match="react_retrieval_source not wired"):
            await runner.run(AgentRequest(interaction_id="r7", query_text="q"))
