from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.application.agents.spec_driven_v1 import (
    SPEC_DRIVEN_VARIANT_ID,
    SpecDrivenRunner,
    _render_spec_block,
)
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
from app.domain.interaction import AgentRequest
from app.domain.spec_driven import AnswerSpec, SpecSlot
from app.domain.tools import ToolResult
from app.ports.llm import LLMResult, LLMTokenDelta, LLMUnavailableError

# spec_driven_v1 — 4-Node 선형 conductor end-to-end(fake). VariantRegistry.build →
# factory → runner 의 실제 선택 경로를 탄다(deps 배선까지 검증).

_SPEC = VariantSpec(variant_id=SPEC_DRIVEN_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"

_SPEC_JSON = json.dumps({
    "intent": "compliance",
    "explicit_references": ["10 CFR 50.46"],
    "governing_normative_class": "binding",
    "required_slots": [
        {"name": "governing_clause",
         "keywords": ["10 CFR 50.46", "ECCS acceptance criteria"], "required": True},
        {"name": "requirement_text",
         "keywords": ["peak cladding temperature"], "required": True},
    ],
    "answer_structure": "지배조문→정량 요건",
})
# N2 가 명시적 참조를 *빠뜨린* 쿼리(safety net 검증용 — ref 가 자동 합류돼야 한다).
_QUERIES_JSON = json.dumps({
    "queries": [
        {"slot_name": "governing_clause",
         "query_text": "ECCS acceptance criteria", "collection": "10CFR"},
        {"slot_name": "requirement_text",
         "query_text": "peak cladding temperature 2200 F"},
    ]
})
_ANSWER = "ECCS 요건은 PCT 2200°F 이하다 [cite-1]."
# N0 Triage 스크립트 — route=retrieval(기존 경로) / route=general(우회). N0 가 첫 generate.
_TRIAGE_RETRIEVAL = json.dumps(
    {"rationale": "특정 조문 지칭", "references_specifics": True, "route": "retrieval"}
)
_TRIAGE_GENERAL = json.dumps(
    {"rationale": "일반 개념 — 추론 가능", "references_specifics": False, "route": "general"}
)
# general 분기는 무근거이므로 [cite-N] 가 있으면 결정론 제거돼야 한다(검증용으로 마커 삽입).
_GENERAL_ANSWER = "심층방어는 다중 독립 방벽으로 안전을 확보하는 개념이다 [cite-1]."


class _ScriptLLM:
    """순차 generate() 스크립트(N1 spec → N2 queries → 비스트림 N4) + generate_stream
    (스트림 N4). _SpecLLM(test_answer_spec_intake) idiom 확장."""

    def __init__(self, *, gen_texts: list[str], stream_text: str = _ANSWER,
                 model_id: str = "fake") -> None:
        self._gen = list(gen_texts)
        self._i = 0
        self._stream = stream_text
        self.model_id = model_id

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        t = self._gen[min(self._i, len(self._gen) - 1)]
        self._i += 1
        return LLMResult(text=t, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None,
                              grammar=None) -> AsyncIterator[LLMTokenDelta]:
        yield LLMTokenDelta(content=self._stream, model_id=self.model_id,
                            token_usage={"prompt_tokens": 1, "completion_tokens": 5})

    async def generate_with_tools(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


class _UnavailableGenLLM(_ScriptLLM):
    """N0/N1/N2(generate)는 정상, N4 Generation(generate/stream)에서만 unavailable."""

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        if self._i >= 3:  # N0·N1·N2 후 N4(4번째 generate)에서만 실패.
            raise LLMUnavailableError("down")
        return await super().generate(prompt, model_options=model_options, grammar=grammar)

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):
        raise LLMUnavailableError("down")
        yield  # pragma: no cover


class _EmptyRetriever:
    """retriever.search 가 0건 반환 — gap-answer 경로 검증용."""

    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        return ToolResult(tool_name="retriever.search", tool_version="v1",
                          status="success", output={"chunks": []},
                          latency_ms=0, input_hash="x")


class _SpyRetriever(_EmptyRetriever):
    """호출 횟수를 센다 — general 분기가 retrieval.search 를 0회 호출하는지 검증용."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, tool_input, context) -> ToolResult:
        self.calls += 1
        return await super().invoke(tool_input, context)


def _tool_registry_yaml(root: Path) -> Path:
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
    }}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _deps(tmp: Path, *, llm, retriever=None) -> AgentDeps:
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    tools = {
        "retrieval.search": RetrievalSearchTool(
            retriever=retriever or LocalRetrieverTool(), reranker=IdentityReranker()
        ),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"fake": llm}, default_id="fake")
    return AgentDeps(
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        llm_router=llm_router,
        utility_llm=llm,  # 동일 인스턴스 — N1/N2/N4 generate 순차 cursor.
        tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_answer_spec_source=SpecDrivenAnswerSpecSource(_REPO_PROMPTS),
        spec_driven_query_source=SpecDrivenQuerySource(_REPO_PROMPTS),
        spec_driven_generation_source=SpecDrivenGenerationSource(_REPO_PROMPTS),
        spec_driven_triage_source=SpecDrivenTriageSource(_REPO_PROMPTS),
        spec_driven_general_source=SpecDrivenGeneralSource(_REPO_PROMPTS),
        tunables={
            "citation_contract_path": str(_CONTRACT),
            "retriever_top_k": 3,
            "spec_driven_max_queries": 6,
        },
    )


def _script(gen_texts: list[str] | None = None) -> _ScriptLLM:
    # N0 Triage(retrieval) 가 첫 generate — 기존 retrieval 경로를 그대로 탄다.
    return _ScriptLLM(
        gen_texts=gen_texts or [_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _ANSWER]
    )


def _build(tmp: Path, llm, retriever=None) -> SpecDrivenRunner:
    return VariantRegistry.build(SPEC_DRIVEN_VARIANT_ID, _SPEC,
                                 _deps(tmp, llm=llm, retriever=retriever))


def _event(tmp: Path) -> dict:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def _req() -> AgentRequest:
    return AgentRequest(interaction_id="ix1",
                        query_text="10 CFR 50.46 ECCS 요건은?", model="fake")


def test_variant_is_registered() -> None:
    assert SPEC_DRIVEN_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_end_to_end_grounded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        assert resp.answer_text == _ANSWER
        assert len(resp.citations) > 0  # LocalRetriever fixtures → 근거 있음.
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["evidence_gap"] is False
        assert pin["spec"]["intent"] == "compliance"
        assert pin["spec"]["explicit_references"] == ["10 CFR 50.46"]
        assert pin["spec"]["method"] == "llm"
        assert pin["formulation"]["num_queries"] == 2


@pytest.mark.asyncio
async def test_explicit_reference_lands_in_query_verbatim() -> None:
    # N2 가 첫 쿼리에서 "10 CFR 50.46" 을 빠뜨렸어도 safety net 이 verbatim 합류시킨다.
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        await runner.run(_req())
        queries = _event(tmp)["query_understanding"]["spec_driven"]["formulation"]["queries"]
        joined = " ".join(q["query_text"] for q in queries)
        assert "10 CFR 50.46" in joined
        # collection boost 가 결정론적으로 유도된다(10 CFR → 10CFR).
        assert any(q["target"].get("collection") == ["10CFR"] for q in queries)


@pytest.mark.asyncio
async def test_gap_answer_on_zero_chunks_not_refusal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script(), retriever=_EmptyRetriever())
        resp = await runner.run(_req())
        assert resp.refusal_reason is None  # gap-answer 는 거부 아님(사용자 #3).
        assert resp.citations == ()  # 근거 0건 → 인용 없음.
        # 무근거 [cite-N] 마커는 결정론 backstop 으로 제거된다(advisor #2).
        assert "[cite-" not in resp.answer_text
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["evidence_gap"] is True
        assert pin["retrieval"]["num_chunks"] == 0


@pytest.mark.asyncio
async def test_n1_unparseable_falls_back() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp),
                        _script([_TRIAGE_RETRIEVAL, "not json", _QUERIES_JSON, _ANSWER]))
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["spec"]["method"] == "fallback"
        # fallback spec 도 쿼리·답을 낸다(silent degrade 아님, method 기록).


@pytest.mark.asyncio
async def test_llm_unavailable_during_generation_refuses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = _UnavailableGenLLM(
            gen_texts=[_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _ANSWER])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(_req())
        assert resp.refusal_reason == "llm_unavailable"


@pytest.mark.asyncio
async def test_run_stream_emits_steps_tokens_then_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        kinds = []
        final = None
        async for ev in runner.run_stream(_req()):
            kinds.append(ev.kind)
            if ev.kind == "final":
                final = ev.payload["response"]
        assert "step" in kinds and "token" in kinds and "final" in kinds
        assert final is not None and final.refusal_reason is None


def test_gap_block_present_only_on_evidence_gap_and_language_recency() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        spec = AnswerSpec(
            intent="compliance", explicit_references=("10 CFR 50.46",),
            required_slots=(SpecSlot(name="governing_clause", keywords=("x",)),),
            answer_structure="a→b", governing_normative_class="binding",
        )
        pack = runner._context_builder.build(
            interaction_id="ix", query_text="q", chat_history=(),
            conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
            entities={}, chunks=[], memory_refs=(), tool_result_refs=(),
        )
        grounded = runner._render_generation_prompt("q", pack, spec, evidence_gap=False)
        gap = runner._render_generation_prompt("q", pack, spec, evidence_gap=True)
        assert "# ANSWER SPEC" in grounded
        assert "# EVIDENCE GAP" not in grounded
        assert "# EVIDENCE GAP (NO RESULTS)" in gap
        # 언어 규칙 trailer 는 최고 recency(맨 끝).
        assert grounded.rstrip().endswith("verbatim.")


def test_render_spec_block_shape() -> None:
    spec = AnswerSpec(intent="definition", explicit_references=("RG 1.157",),
                      required_slots=(SpecSlot(name="definition", keywords=("a",)),))
    block = _render_spec_block(spec)
    assert "intent: definition" in block
    assert "explicit_references: RG 1.157" in block


# === N0 Triage / N4-G General Generation (RAG 비대상 도메인 질의 우회) =============

def _req_general() -> AgentRequest:
    return AgentRequest(interaction_id="ixg",
                        query_text="심층방어(defense in depth)의 기본 개념은?",
                        model="fake")


@pytest.mark.asyncio
async def test_general_route_bypasses_retrieval() -> None:
    # N0 가 route=general → N1/N2/N3 우회, retrieval.search 0회. 1급 outcome.
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpyRetriever()
        # general 분기 generate cursor: N0(triage) → N4-G(answer). 2콜.
        llm = _ScriptLLM(gen_texts=[_TRIAGE_GENERAL, _GENERAL_ANSWER])
        runner = _build(Path(tmp), llm, retriever=spy)
        resp = await runner.run(_req_general())
        assert spy.calls == 0  # 검색 도구 한 번도 안 부른다.
        assert resp.refusal_reason is None
        assert resp.regulatory_grounding == "parametric"  # grounded 아님 — 감사 구별.
        assert resp.citations == ()
        # 무근거 [cite-N] 마커는 결정론 backstop 으로 제거된다.
        assert "[cite-" not in resp.answer_text
        assert resp.answer_text.startswith("심층방어는 다중 독립 방벽")
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["route"] == "general"
        assert pin["triage"]["route"] == "general"
        assert pin["triage"]["method"] == "llm"
        # general 분기는 spec/formulation/retrieval 백을 남기지 않는다(노드 미실행).
        assert "spec" not in pin and "formulation" not in pin


@pytest.mark.asyncio
async def test_triage_unparseable_degrades_to_retrieval() -> None:
    # N0 응답 파싱불가 → 라우팅 근거 없음 → 안전 degrade(retrieval). 라우팅 규칙 아님.
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp),
                        _script(["not json", _SPEC_JSON, _QUERIES_JSON, _ANSWER]))
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["route"] == "retrieval"  # degrade 로 검색 경로.
        assert pin["triage"]["method"] == "fallback"
        assert pin["spec"]["method"] == "llm"  # 이후 N1 정상.


@pytest.mark.asyncio
async def test_general_route_streams_tokens_then_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = _ScriptLLM(gen_texts=[_TRIAGE_GENERAL], stream_text=_GENERAL_ANSWER)
        runner = _build(Path(tmp), llm, retriever=_SpyRetriever())
        kinds = []
        final = None
        async for ev in runner.run_stream(_req_general()):
            kinds.append(ev.kind)
            if ev.kind == "final":
                final = ev.payload["response"]
        assert "step" in kinds and "token" in kinds and "final" in kinds
        assert final is not None and final.regulatory_grounding == "parametric"


def test_render_general_prompt_no_context_block() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        text = runner._render_general_prompt("심층방어란?")
        assert "# CONTEXT" not in text  # 근거 블록 없음.
        assert "# ANSWER SPEC" not in text
        assert "# QUERY\n심층방어란?" in text
        # 출력-언어 trailer 가 최고 recency(맨 끝).
        assert "# RESPONSE LANGUAGE" in text
        assert text.rstrip().endswith("Korean answer).")
