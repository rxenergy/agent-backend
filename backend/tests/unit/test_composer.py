from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import AsyncIterator

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.application.agents.composer import COMPOSER_VARIANT_ID, ComposerRunner
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import AgentDeps, VariantRegistry
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
from app.domain.tools import ToolResult
from app.ports.llm import LLMResult, LLMTokenDelta, LLMUnavailableError

# composer — N0~N3.5 계승 + N4 슬롯 파이프라인 end-to-end(fake). VariantRegistry.build →
# factory → ComposerRunner 의 실제 선택 경로를 탄다(deps 배선·등록 포함).

_SPEC = VariantSpec(variant_id=COMPOSER_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"

_TRIAGE_RETRIEVAL = json.dumps(
    {"rationale": "특정 조문 지칭", "references_specifics": True, "route": "retrieval"}
)
_TRIAGE_GENERAL = json.dumps(
    {"rationale": "일반 개념", "references_specifics": False, "route": "general"}
)
# 두 required 슬롯 → 두 슬롯 생성 콜 + 종합 콜.
_SPEC_JSON = json.dumps({
    "intent": "compliance",
    "explicit_references": ["10 CFR 50.46"],
    "governing_normative_class": "binding",
    "required_slots": [
        {"name": "governing_clause", "facet": "requirement",
         "keywords": ["10 CFR 50.46", "ECCS acceptance criteria"], "required": True},
        {"name": "requirement_text", "facet": "quantitative_limit",
         "keywords": ["peak cladding temperature"], "required": True},
    ],
    "answer_structure": "지배조문→정량 요건",
})
_QUERIES_JSON = json.dumps({
    "queries": [
        {"slot_name": "governing_clause",
         "query_text": "ECCS acceptance criteria", "collection": "10CFR"},
        {"slot_name": "requirement_text",
         "query_text": "peak cladding temperature 2200 F"},
    ]
})
_SLOT1 = "**10 CFR 50.46** 은 ECCS 성능을 요구한다 [cite-0]."
_SLOT2 = "최대 피복재 온도는 2200°F 이하여야 한다 [cite-0]."
# 종합은 본문 재출력이 아니라 "정리+다음액션" 닫음 블록(슬롯은 이미 스트리밍됨).
_SYNTH = "## 핵심 정리\n- 요건과 한계가 연결된다.\n\n## 다음 단계 제안\n- SER 조건 확인."


class _ComposerScriptLLM:
    """순차 스크립트 LLM: N0 triage → N1 spec → N2 queries → 슬롯1 → 슬롯2 → 종합.

    한 *cursor*(_i)를 generate 와 generate_stream 이 공유한다 — emitter 활성 시 N0/N1/N2 는
    stream_capture(generate_stream)로, composer 슬롯/종합은 generate 로 호출되므로 둘이
    같은 순서를 소비해야 스크립트가 어긋나지 않는다(reasoning_capture 가 delta.content 를
    누적·파싱하므로 stream 도 동일 텍스트를 1개 content delta 로 흘린다)."""

    def __init__(self, gen_texts: list[str], model_id: str = "fake") -> None:
        self._gen = list(gen_texts)
        self._i = 0
        self.model_id = model_id
        self.calls = 0

    def _next(self) -> str:
        t = self._gen[min(self._i, len(self._gen) - 1)]
        self._i += 1
        self.calls += 1
        return t

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        return LLMResult(text=self._next(),
                         token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None,
                              grammar=None) -> AsyncIterator[LLMTokenDelta]:
        yield LLMTokenDelta(content=self._next(), model_id=self.model_id,
                            token_usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def generate_with_tools(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def _tool_registry_yaml(root: Path) -> Path:
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
    }}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


class _EmptyRetriever:
    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        return ToolResult(tool_name="retriever.search", tool_version="v1",
                          status="success", output={"chunks": []},
                          latency_ms=0, input_hash="x")


def _deps(tmp: Path, *, llm, retriever=None, tunables=None) -> AgentDeps:
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
    base_tunables = {
        "citation_contract_path": str(_CONTRACT),
        "retriever_top_k": 3,
        "spec_driven_max_queries": 6,
    }
    base_tunables.update(tunables or {})
    return AgentDeps(
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        llm_router=llm_router,
        utility_llm=llm,
        tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_answer_spec_source=SpecDrivenAnswerSpecSource(_REPO_PROMPTS),
        spec_driven_query_source=SpecDrivenQuerySource(_REPO_PROMPTS),
        spec_driven_generation_source=SpecDrivenGenerationSource(_REPO_PROMPTS),
        spec_driven_triage_source=SpecDrivenTriageSource(_REPO_PROMPTS),
        spec_driven_general_source=SpecDrivenGeneralSource(_REPO_PROMPTS),
        tunables=base_tunables,
    )


def _build(tmp: Path, llm, retriever=None, tunables=None) -> ComposerRunner:
    return VariantRegistry.build(
        COMPOSER_VARIANT_ID, _SPEC,
        _deps(tmp, llm=llm, retriever=retriever, tunables=tunables))


def _event(tmp: Path) -> dict:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def _req() -> AgentRequest:
    return AgentRequest(interaction_id="ix1",
                        query_text="10 CFR 50.46 ECCS 요건은?", model="fake")


def _slotwise_script() -> _ComposerScriptLLM:
    return _ComposerScriptLLM(
        [_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _SLOT1, _SLOT2, _SYNTH])


def test_composer_is_registered() -> None:
    assert COMPOSER_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_slotwise_streams_slots_then_appends_closing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _slotwise_script())
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        # 최종 답 = 슬롯 본문(answer_structure 헤더 + 본문) + 닫음 블록(정리+다음액션).
        # 슬롯 본문이 *먼저*(조기 스트리밍), 종합 닫음 블록이 *뒤에* 붙는다.
        # (슬롯2 의 [cite-0] 은 슬롯2 CONTEXT 서브셋이 cite-0 을 안 가지면 L0 가 제거 →
        #  본문 텍스트만 robust 하게 검증한다.)
        assert "ECCS 성능을 요구한다" in resp.answer_text
        assert "최대 피복재 온도는 2200°F 이하여야 한다" in resp.answer_text
        assert "## 핵심 정리" in resp.answer_text
        assert "## 다음 단계 제안" in resp.answer_text
        # answer_structure("지배조문→정량 요건") 기반 헤더가 슬롯 앞에.
        assert "## 지배조문" in resp.answer_text
        assert "## 정량 요건" in resp.answer_text
        # 본문(슬롯)이 닫음 블록보다 앞에 온다.
        assert (resp.answer_text.index("ECCS 성능을 요구한다")
                < resp.answer_text.index("## 핵심 정리"))
        gen = _event(tmp)["query_understanding"]["spec_driven"]["generation"]
        assert gen["mode"] == "slotwise"
        assert gen["num_slots"] == 2
        assert gen["synthesize"]["mode"] == "model"
        names = [s["name"] for s in gen["slots"]]
        assert names == ["governing_clause", "requirement_text"]
        assert all(s["verdict"]["l0"] in ("pass", "violation") for s in gen["slots"])


@pytest.mark.asyncio
async def test_slots_and_synthesize_streamed_in_order() -> None:
    # run_stream 토큰 이벤트 순서 — 슬롯1 → 슬롯2 → 닫음 블록(종합)이 *모두 토큰으로*
    # 스트리밍되고 순서가 보존되는지. 종합도 토큰 스트림에 실린다(별도 final 본문 아님).
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _slotwise_script())
        tokens: list[str] = []
        async for ev in runner.run_stream(_req()):
            if ev.kind == "token":
                tokens.append(ev.payload["content"])
        joined = "".join(tokens)
        # 두 슬롯 본문 + 종합 닫음 블록이 전부 토큰 스트림에 등장.
        assert "ECCS 성능을 요구한다" in joined
        assert "최대 피복재 온도는 2200°F" in joined
        assert "## 핵심 정리" in joined and "## 다음 단계 제안" in joined
        # 순서: 슬롯1 < 슬롯2 < 종합(닫음 블록).
        assert (joined.index("ECCS 성능을 요구한다")
                < joined.index("최대 피복재 온도는 2200°F")
                < joined.index("## 핵심 정리"))


@pytest.mark.asyncio
async def test_l0_gate_strips_out_of_range_cite() -> None:
    # 슬롯이 그 슬롯 CONTEXT 서브셋 밖 cite(cite-99)를 남기면 L0 게이트가 제거한다.
    bad_slot1 = "근거 없는 주장 [cite-99]."
    script = _ComposerScriptLLM(
        [_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, bad_slot1, _SLOT2, _SYNTH])
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), script, tunables={"composer_slot_verify": "l0"})
        await runner.run(_req())
        gen = _event(tmp)["query_understanding"]["spec_driven"]["generation"]
        s0 = gen["slots"][0]
        assert s0["verdict"]["l0"] == "violation"
        assert "cite-99" in s0["verdict"]["l0_out_of_range"]


@pytest.mark.asyncio
async def test_no_closing_when_synthesize_disabled() -> None:
    # 종합 비활성 → 슬롯 본문만(닫음 블록 없음). 종합 LLM 콜 없음(스크립트 5개로 충분).
    script = _ComposerScriptLLM(
        [_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _SLOT1, _SLOT2])
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), script, tunables={"composer_synthesize": False})
        resp = await runner.run(_req())
        # answer_structure 기반 헤더 + 슬롯 본문, 닫음 블록 없음.
        assert "## 지배조문" in resp.answer_text
        assert "ECCS 성능을 요구한다" in resp.answer_text
        assert "최대 피복재 온도는 2200°F 이하여야 한다" in resp.answer_text
        assert "## 핵심 정리" not in resp.answer_text
        gen = _event(tmp)["query_understanding"]["spec_driven"]["generation"]
        assert gen["synthesize"]["mode"] == "off"


@pytest.mark.asyncio
async def test_gap_answer_falls_back_to_single_path() -> None:
    # 근거 0건 → 슬롯 분해 비대상, 계승한 단일 gap-answer 경로(슬롯 핀 없음).
    script = _ComposerScriptLLM(
        [_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, "근거가 없습니다."])
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), script, retriever=_EmptyRetriever())
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["evidence_gap"] is True
        # 단일 경로라 generation 슬롯 핀이 없다(slotwise 미적용).
        assert "generation" not in pin or pin.get("generation", {}).get("mode") != "slotwise"


@pytest.mark.asyncio
async def test_general_route_inherits_single_answer() -> None:
    # general 분기 — 계승한 _run_general(검색·슬롯 없음).
    script = _ComposerScriptLLM([_TRIAGE_GENERAL, "심층방어 개념 설명."])
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), script)
        resp = await runner.run(_req())
        assert resp.regulatory_grounding == "parametric"
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["route"] == "general"


@pytest.mark.asyncio
async def test_slot_llm_unavailable_refuses() -> None:
    # 슬롯 생성 중 LLM 미가용 → 전체 거부(실패 1급, 부분 답 비노출).
    class _SlotDown(_ComposerScriptLLM):
        async def generate(self, prompt, *, model_options=None, grammar=None):
            if self._i >= 3:  # N0·N1·N2 후 첫 슬롯 생성에서 실패.
                raise LLMUnavailableError("down")
            return await super().generate(prompt, model_options=model_options,
                                          grammar=grammar)

        async def generate_stream(self, prompt, *, model_options=None, grammar=None):
            # 슬롯 본문은 이제 토큰 스트리밍(_slot_generate_stream)으로 생성된다 →
            # N0·N1·N2(generate) 후 첫 슬롯 스트림에서 미가용을 던져야 거부 경로를 탄다.
            if self._i >= 3:
                raise LLMUnavailableError("down")
            async for d in super().generate_stream(prompt, model_options=model_options,
                                                   grammar=grammar):
                yield d

    script = _SlotDown([_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON])
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), script)
        resp = await runner.run(_req())
        assert resp.refusal_reason == "llm_unavailable"
