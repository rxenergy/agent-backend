from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import AsyncIterator

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.session_store.in_memory import InMemorySessionStateStore
from app.adapters.tools.memory_session_local import SessionLoadTool, SessionUpdateTool
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.application.agents.spec_driven_v1 import SPEC_DRIVEN_VARIANT_ID, SpecDrivenRunner
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.spec_driven_source import (
    SpecDrivenAnswerSpecSource,
    SpecDrivenGeneralSource,
    SpecDrivenGenerationSource,
    SpecDrivenQuerySource,
    SpecDrivenTriageSource,
)
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest, ChatTurn
from app.ports.llm import LLMResult, LLMTokenDelta

# spec_driven_v1 멀티턴 세션 메모리 — N-1 load / 2단 게이트 / N5 update 의 실제 경로를
# fake LLM·in-memory store 로 end-to-end 검증한다. 설계:
# docs/plans/spec_driven_session_memory.design.v1.md.

_SPEC = VariantSpec(variant_id=SPEC_DRIVEN_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"

_TRIAGE = json.dumps(
    {"rationale": "특정 조문", "references_specifics": True, "route": "retrieval"}
)


def _spec_json(refs, topic, authority="binding") -> str:
    return json.dumps({
        "intent": "compliance",
        "explicit_references": refs,
        "governing_normative_class": authority,
        "topic_label": topic,
        "required_slots": [
            {"name": "governing_clause", "keywords": refs or ["ECCS"],
             "required": True},
        ],
        "answer_structure": "지배조문",
    })


_QUERIES = json.dumps({"queries": [
    {"slot_name": "governing_clause", "query_text": "ECCS acceptance criteria"},
]})


class _ScriptLLM:
    """턴마다 N0→N1→N2→N4 generate 4회 + stream N4. 여러 턴을 위해 gen 큐를 이어붙인다."""

    def __init__(self, gen_texts: list[str], stream_text: str = "답변 [cite-1]") -> None:
        self._gen = list(gen_texts)
        self._i = 0
        self._stream = stream_text
        self.model_id = "fake"
        self.prompts: list[str] = []

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        self.prompts.append(prompt)
        t = self._gen[min(self._i, len(self._gen) - 1)]
        self._i += 1
        return LLMResult(text=t, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None,
                              grammar=None) -> AsyncIterator[LLMTokenDelta]:
        self.prompts.append(prompt)
        yield LLMTokenDelta(content=self._stream, model_id=self.model_id,
                            token_usage={"prompt_tokens": 1, "completion_tokens": 2})

    async def generate_with_tools(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def _tool_registry_yaml(root: Path) -> Path:
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
        "memory.session_load": {"version": "v1", "adapter": "postgres",
                                "timeout_ms": 1000, "retry": 0, "required": False},
        "memory.session_update": {"version": "v1", "adapter": "postgres",
                                  "timeout_ms": 1000, "retry": 0, "required": False},
    }}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _deps(tmp: Path, *, llm, store, enabled: bool) -> AgentDeps:
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    tools = {
        "retrieval.search": RetrievalSearchTool(
            retriever=LocalRetrieverTool(), reranker=IdentityReranker()
        ),
        "memory.session_load": SessionLoadTool(store),
        "memory.session_update": SessionUpdateTool(store, ttl_days=90),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"fake": llm}, default_id="fake")
    return AgentDeps(
        recorder=recorder, event_sink=sink, app_profile="local",
        llm_router=llm_router, utility_llm=llm, tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_answer_spec_source=SpecDrivenAnswerSpecSource(_REPO_PROMPTS),
        spec_driven_query_source=SpecDrivenQuerySource(_REPO_PROMPTS),
        spec_driven_generation_source=SpecDrivenGenerationSource(_REPO_PROMPTS),
        spec_driven_triage_source=SpecDrivenTriageSource(_REPO_PROMPTS),
        spec_driven_general_source=SpecDrivenGeneralSource(_REPO_PROMPTS),
        summarizer=None,
        tunables={
            "retriever_top_k": 3,
            "spec_driven_max_queries": 6,
            "spec_driven_session_memory_enabled": enabled,
        },
    )


def _build(tmp: Path, llm, store, *, enabled: bool) -> SpecDrivenRunner:
    return VariantRegistry.build(SPEC_DRIVEN_VARIANT_ID, _SPEC,
                                 _deps(tmp, llm=llm, store=store, enabled=enabled))


def _events(tmp: str) -> list[dict]:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    out: list[dict] = []
    for f in sorted(root.rglob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            out.append(json.loads(line))
    return out


def _req(ix: str, query: str, history=()) -> AgentRequest:
    return AgentRequest(interaction_id=ix, query_text=query, model="fake",
                        session_id="S1", chat_history=tuple(history))


# run() 은 emitter 비활성이라 N4 도 generate() — 턴당 generate 4회(N0·N1·N2·N4).
_A = "답변 [cite-1]"


@pytest.mark.asyncio
async def test_disabled_does_not_touch_session() -> None:
    # 기본(비활성) — session_load/update 도구를 호출하지 않고 단일턴으로 동작한다.
    with tempfile.TemporaryDirectory() as tmp:
        store = InMemorySessionStateStore()
        llm = _ScriptLLM([_TRIAGE, _spec_json(["10 CFR 50.46"], "eccs"), _QUERIES, _A])
        runner = _build(Path(tmp), llm, store, enabled=False)
        await runner.run(_req("ix1", "10 CFR 50.46 ECCS 요건은?"))
        assert await store.get("S1") is None  # update 미호출
        ev = _events(tmp)[0]
        sess = ev["query_understanding"]["spec_driven"]["session"]
        assert sess["enabled"] is False


@pytest.mark.asyncio
async def test_first_turn_persists_no_inject() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = InMemorySessionStateStore()
        llm = _ScriptLLM([_TRIAGE, _spec_json(["10 CFR 50.46"], "eccs"), _QUERIES, _A])
        runner = _build(Path(tmp), llm, store, enabled=True)
        await runner.run(_req("ix1", "10 CFR 50.46 ECCS 요건은?"))
        ev = _events(tmp)[0]
        sess = ev["query_understanding"]["spec_driven"]["session"]
        # 첫 턴(history 없음) → 사전·사후 게이트 모두 미주입.
        assert sess["pre_gate"]["inject"] is False
        assert sess["pre_gate"]["reason"] == "no_history"
        assert sess["post_gate"]["inject"] is False
        # N5 가 세션을 적재했다 — refs 누적 + turn_count=1.
        state = await store.get("S1")
        assert state is not None
        assert state.turn_count == 1
        assert "10 CFR 50.46" in [r.ref_id for r in state.tracked_references]
        assert state.last_variant_id == SPEC_DRIVEN_VARIANT_ID


@pytest.mark.asyncio
async def test_follow_up_injects_and_carries_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = InMemorySessionStateStore()
        # 턴1: 10 CFR 50.46/eccs. 턴2: 순수 anaphora("그 중 PCT") — 새 명시참조 없음.
        llm = _ScriptLLM([
            _TRIAGE, _spec_json(["10 CFR 50.46"], "eccs"), _QUERIES, _A,    # 턴1
            _TRIAGE, _spec_json([], "eccs"), _QUERIES, _A,                  # 턴2
        ])
        runner = _build(Path(tmp), llm, store, enabled=True)
        await runner.run(_req("ix1", "10 CFR 50.46 ECCS 요건은?"))
        await runner.run(_req("ix2", "그 중 PCT 한계는?",
                              history=[ChatTurn(role="user",
                                                content="10 CFR 50.46 ECCS 요건은?")]))
        ev2 = _events(tmp)[1]
        sess = ev2["query_understanding"]["spec_driven"]["session"]
        # 사전 게이트 통과(history+동일 variant) + 사후 게이트 follow_up(route/topic 유지,
        # current refs 비어 overlap 게이트 미적용).
        assert sess["pre_gate"]["inject"] is True
        assert sess["post_gate"]["inject"] is True
        assert sess["post_gate"]["reason"] == "follow_up"
        # 턴2 N0/N1 프롬프트에 PRIOR CONTEXT(이전 참조)가 동반됐다.
        assert any("PRIOR CONTEXT" in p and "10 CFR 50.46" in p for p in llm.prompts)
        # 이벤트 memory_ids_used 에 세션 ref 가 기록(주입 → MemoryRef).
        assert "S1" in ev2.get("memory_ids_used", [])


@pytest.mark.asyncio
async def test_topic_shift_suppresses_injection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = InMemorySessionStateStore()
        # 턴1: eccs/binding. 턴2: 다른 주제(seismic) + 다른 참조 → topic/ref shift.
        llm = _ScriptLLM([
            _TRIAGE, _spec_json(["10 CFR 50.46"], "eccs"), _QUERIES, _A,        # 턴1
            _TRIAGE, _spec_json(["10 CFR 100.23"], "seismic"), _QUERIES, _A,    # 턴2
        ])
        runner = _build(Path(tmp), llm, store, enabled=True)
        await runner.run(_req("ix1", "10 CFR 50.46 ECCS 요건은?"))
        await runner.run(_req("ix2", "내진 설계 기준은?",
                              history=[ChatTurn(role="user",
                                                content="10 CFR 50.46 ECCS 요건은?")]))
        ev2 = _events(tmp)[1]
        sess = ev2["query_understanding"]["spec_driven"]["session"]
        # 사전 통과(history)했으나 사후 게이트가 topic/ref shift 로 차단.
        assert sess["post_gate"]["inject"] is False
        assert sess["post_gate"]["reason"] in (
            "topic_shift", "reference_overlap_below_threshold"
        )
        assert "S1" not in ev2.get("memory_ids_used", [])


@pytest.mark.asyncio
async def test_session_load_graceful_when_tool_missing() -> None:
    # session 도구 미배선이어도 graceful(단일턴 degrade) — enabled 라도 죽지 않는다.
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=str(Path(tmp) / "events"), prefix="t")
        recorder = EventRecorder(sink, app_profile="local")
        registry = ToolRegistry.from_yaml(_tool_registry_yaml(Path(tmp)))
        tools = {"retrieval.search": RetrievalSearchTool(
            retriever=LocalRetrieverTool(), reranker=IdentityReranker())}
        executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
        llm = _ScriptLLM([_TRIAGE, _spec_json(["10 CFR 50.46"], "eccs"), _QUERIES, _A])
        deps = AgentDeps(
            recorder=recorder, event_sink=sink, app_profile="local",
            llm_router=LLMRouter(pool={"fake": llm}, default_id="fake"),
            utility_llm=llm, tool_executor=executor,
            context_builder=ContextBuilder(capture_mode="snippets"),
            spec_driven_answer_spec_source=SpecDrivenAnswerSpecSource(_REPO_PROMPTS),
            spec_driven_query_source=SpecDrivenQuerySource(_REPO_PROMPTS),
            spec_driven_generation_source=SpecDrivenGenerationSource(_REPO_PROMPTS),
            spec_driven_triage_source=SpecDrivenTriageSource(_REPO_PROMPTS),
            spec_driven_general_source=SpecDrivenGeneralSource(_REPO_PROMPTS),
            tunables={"spec_driven_session_memory_enabled": True},
        )
        runner = VariantRegistry.build(SPEC_DRIVEN_VARIANT_ID, _SPEC, deps)
        resp = await runner.run(_req("ix1", "10 CFR 50.46 ECCS 요건은?"))
        assert resp.refusal_reason is None  # 도구 미배선에도 답변 산출
