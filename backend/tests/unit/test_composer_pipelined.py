"""composer_pipelined — 슬롯 검색-생성 파이프라이닝(배리어 제거) end-to-end(fake).

설계: docs/plans/spec_driven_slot_pipeline_streaming.design.v1.md. 검증 포인트:
  - 슬롯별 검색-검증(v2 _run_slot_pipeline) + 슬롯 단위 생성/스트리밍(composer)이 결합돼
    동작한다(VariantRegistry.build → factory → ComposerPipelinedRunner 실제 경로).
  - cite-N 은 SlotCitationAllocator 가 생성 *전* 에 전역 단일 공간으로 배정(슬롯 간 공유
    chunk 는 단일 cite 재사용) → 서로 다른 근거가 다른 번호로 분리, 같은 근거는 단일화.
  - 슬롯 검증 근거가 *그 슬롯 본문 직전* reasoning 에 실린다(스트리밍 순서).
  - verify 도구 미배선 → 슬롯 fallback(전량 necessary, 단일노드 degrade).
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.application.agents.composer_pipelined import (
    COMPOSER_PIPELINED_VARIANT_ID,
)
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
from app.ports.llm import LLMResult, LLMTokenDelta

_SPEC = VariantSpec(variant_id=COMPOSER_PIPELINED_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"

_TRIAGE_RETRIEVAL = json.dumps(
    {"rationale": "특정 조문", "references_specifics": True, "route": "retrieval"}
)
# 두 required 슬롯 → 두 슬롯 검색-검증 future + 두 슬롯 생성 콜 + 종합 콜.
_SPEC_JSON = json.dumps({
    "intent": "compliance",
    "explicit_references": ["10 CFR 50.46"],
    "governing_normative_class": "binding",
    "required_slots": [
        {"name": "governing_clause", "facet": "requirement",
         "keywords": ["10 CFR 50.46"], "required": True},
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
# 슬롯 본문은 *프롬프트 CONTEXT 에 노출된 전역 cite-N* 을 인용한다(실모델 거동 모사 —
# SlotCitationAllocator 가 생성 *전* 에 전역 번호를 배정하므로, 슬롯2 CONTEXT 는 cite-1 을
# 노출하고 모델은 [cite-1] 을 낸다). 본문 텍스트 템플릿의 {cite} 는 _CiteAwareLLM 이 채운다.
_SLOT1 = "**10 CFR 50.46** ECCS 성능 요구 [{cite}]."
_SLOT2 = "최대 피복재 온도 2200°F 이하 [{cite}]."
_SYNTH = "## 핵심 정리\n- 요건·한계 연결.\n\n## 다음 단계 제안\n- SER 확인."

# 슬롯 프롬프트의 *실제* # CONTEXT 섹션에서 첫 cite-N 을 뽑는다(실모델이 보는 전역 번호).
# `# CONTEXT` 문구는 PRIOR SECTIONS 안내문에도 등장하므로 *마지막* `# CONTEXT\n` 헤더
# 뒤만 본다(실제 컨텍스트 섹션은 프롬프트 후반 recency 위치).
_CITE_RE = re.compile(r"\[(cite-\d+)\]")


def _first_context_cite(prompt: str) -> str | None:
    idx = prompt.rfind("\n# CONTEXT\n")
    if idx < 0:
        return None
    m = _CITE_RE.search(prompt, idx)
    return m.group(1) if m else None


class _ScriptLLM:
    """순차 스크립트: N0→N1→N2(stream_capture) → 슬롯1→슬롯2(stream) → 종합(stream).

    슬롯 검색-검증 future 가 N2 직후 *동시* 발사되나 검증은 도구(_FakeVerifyTool)가 처리하므로
    이 LLM 의 cursor 와 무관하다 — cursor 는 N0/N1/N2 + 슬롯 생성 + 종합만 소비한다."""

    def __init__(self, gen_texts: list[str]) -> None:
        self._gen = list(gen_texts)
        self._i = 0
        self.model_id = "fake"

    def _next(self, prompt: str = "") -> str:
        t = self._gen[min(self._i, len(self._gen) - 1)]
        self._i += 1
        # 슬롯 본문 템플릿({cite})은 프롬프트 CONTEXT 의 첫 전역 cite-N 으로 채운다 — 실모델이
        # CONTEXT 에 노출된 번호를 인용하는 거동 모사(allocator 가 전역 배정한 그 번호).
        if "{cite}" in t:
            t = t.format(cite=_first_context_cite(prompt) or "cite-0")
        return t

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        return LLMResult(text=self._next(prompt),
                         token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None,
                              grammar=None) -> AsyncIterator[LLMTokenDelta]:
        yield LLMTokenDelta(content=self._next(prompt), model_id=self.model_id,
                            token_usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def generate_with_tools(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


class _SlotRetriever:
    """슬롯 쿼리별로 다른 청크 반환(슬롯 귀속이 분리되는지 확인).
    - governing_clause 쿼리(ECCS) → g1
    - requirement_text 쿼리(peak cladding) → r1
    멀티홉/2차 검색(source_id 필터)은 빈 결과(2차 없음 — 생성 경로 집중)."""

    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        ti = tool_input if isinstance(tool_input, dict) else {}
        qtext = ti.get("query_text", "")
        filters = ti.get("filters") or {}
        if filters.get("source_id"):
            chunks: list[dict[str, Any]] = []
        elif "cladding" in qtext or "2200" in qtext:
            chunks = [{"chunk_id": "r1", "document_id": "D2", "score": 0.85,
                       "snippet": "peak cladding temperature limit body", "source_id": "S2"}]
        else:
            chunks = [{"chunk_id": "g1", "document_id": "D1", "score": 0.9,
                       "snippet": "ECCS governing clause body", "source_id": "S1"}]
        return ToolResult(tool_name="retriever.search", tool_version="v1",
                          status="success", output={"chunks": chunks},
                          latency_ms=0, input_hash="x", trace_id="")


class _FakeVerifyTool:
    """Node1 — 1차 청크 전부 necessary, 멀티홉 없음(2차 검색 안 함)."""

    name = "retrieval.verify_slot"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        chunks = (tool_input.get("chunks") if isinstance(tool_input, dict)
                  else getattr(tool_input, "chunks", [])) or []
        ids = [(c.get("chunk_id") if isinstance(c, dict) else c.chunk_id) for c in chunks]
        slot = (tool_input.get("slot_name") if isinstance(tool_input, dict) else "")
        return ToolResult(
            tool_name="retrieval.verify_slot", tool_version="v1", status="success",
            output={"necessary_chunk_ids": ids, "multihop_chunk_ids": [],
                    "rationale": f"{slot} 청크 전부 필요", "method": "llm"},
            latency_ms=0, input_hash="x", output_hash="y", trace_id="",
        )


def _tool_registry_yaml(root: Path, *, with_verify: bool) -> Path:
    tools: dict[str, Any] = {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
    }
    if with_verify:
        tools["retrieval.verify_slot"] = {"version": "v1", "adapter": "vllm_verify",
                                          "timeout_ms": 6000, "retry": 0, "required": False}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump({"tools": tools}))
    return p


def _deps(tmp: Path, *, llm, with_verify: bool) -> AgentDeps:
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp, with_verify=with_verify))
    tools: dict[str, Any] = {"retrieval.search": _SlotRetriever()}
    if with_verify:
        tools["retrieval.verify_slot"] = _FakeVerifyTool()
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"fake": llm}, default_id="fake")
    return AgentDeps(
        recorder=recorder, event_sink=sink, app_profile="local",
        llm_router=llm_router, utility_llm=llm, secondary_llm=llm,
        tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_answer_spec_source=SpecDrivenAnswerSpecSource(_REPO_PROMPTS),
        spec_driven_query_source=SpecDrivenQuerySource(_REPO_PROMPTS),
        spec_driven_generation_source=SpecDrivenGenerationSource(_REPO_PROMPTS),
        spec_driven_triage_source=SpecDrivenTriageSource(_REPO_PROMPTS),
        spec_driven_general_source=SpecDrivenGeneralSource(_REPO_PROMPTS),
        tunables={"citation_contract_path": str(_CONTRACT), "retriever_top_k": 3,
                  "spec_driven_max_queries": 6, "spec_driven_max_context_chunks": 10,
                  "spec_driven_v2_verify_concurrency": 3,
                  # v2 composer source 미배선이라 base v1 source 로 graceful fallback.
                  "composer_prompts_v2": False},
    )


def _script() -> _ScriptLLM:
    return _ScriptLLM([_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON,
                       _SLOT1, _SLOT2, _SYNTH])


def _event(tmp: Path) -> dict:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def _req() -> AgentRequest:
    return AgentRequest(interaction_id="cp1",
                        query_text="10 CFR 50.46 ECCS 요건은?", model="fake")


def test_composer_pipelined_is_registered() -> None:
    assert COMPOSER_PIPELINED_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_pipelined_slot_generation_with_global_cite_remap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = VariantRegistry.build(
            COMPOSER_PIPELINED_VARIANT_ID, _SPEC,
            _deps(Path(tmp), llm=_script(), with_verify=True))
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        # 두 슬롯 본문이 헤더와 함께 스트리밍된 본문에 있다.
        assert "## 지배조문" in resp.answer_text
        assert "## 정량 요건" in resp.answer_text
        # 종합(닫음 블록)이 본문 뒤에.
        assert "## 핵심 정리" in resp.answer_text
        # cite-N 전역 배정 — SlotCitationAllocator 가 생성 *전* 에 슬롯1=cite-0(g1),
        # 슬롯2=cite-1(r1) 로 매겨 서로 다른 근거가 다른 번호로 분리된다(둘 다 cite-0 이면
        # 통합 실패 — 이 변형이 고치는 문제).
        cite_ids = {c.citation_id for c in resp.citations}
        assert "cite-0" in cite_ids and "cite-1" in cite_ids
        # 본문에 두 전역 cite 가 모두 등장(모델이 CONTEXT 의 전역 번호를 그대로 인용).
        assert "[cite-0]" in resp.answer_text and "[cite-1]" in resp.answer_text
        # 같은 번호가 다른 근거에 중복 매겨지지 않는다.
        assert resp.answer_text.count("[cite-0]") == 1
        assert resp.answer_text.count("[cite-1]") == 1
        # 재현 핀 — pipelined 검색 + 슬롯 생성.
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["retrieval"]["pipelined"] is True
        assert pin["retrieval"]["cite_scope"] == "global_allocated"
        assert pin["generation"]["mode"] == "slotwise_pipelined"
        assert pin["generation"]["num_slots"] == 2
        assert pin["verify"]["node1"] is True
        assert pin["verify"]["total_necessary"] == 2  # g1 + r1
        # 노드 분리 가시화 — relevance(verify_slot)=utility_llm(=fake) 모델 id 핀.
        # 노드 분리 가시화 — relevance(verify_slot)·multihop(follow_up) 둘 다 worker
        # (secondary_llm=fake) 노드에서 돈다(profiles.py 배선). main(생성)과 물리 분리.
        assert pin["retrieval"]["relevance_llm_id"] == "fake"
        assert pin["retrieval"]["multihop_llm_id"] == "fake"


@pytest.mark.asyncio
async def test_slot_verify_thinking_not_streamed_but_rationale_in_pin() -> None:
    # 검색-검증(Node1) thinking 의 UI 노출은 비활성화 — 생성과 병렬로 도는 파이프라인에서
    # 본문 스트림과 섞여 답변이 깨지던 현상 제거. 검증 근거(rationale)는 재현 핀에 남아
    # 관측/재현은 영향 없다.
    with tempfile.TemporaryDirectory() as tmp:
        runner = VariantRegistry.build(
            COMPOSER_PIPELINED_VARIANT_ID, _SPEC,
            _deps(Path(tmp), llm=_script(), with_verify=True))
        reasoning_parts: list[str] = []
        async for ev in runner.run_stream(_req()):
            if ev.kind == "reasoning":
                reasoning_parts.append(ev.payload.get("content", ""))
        reasoning = "".join(reasoning_parts)
        # UI thinking 에 슬롯 검증 블록이 더는 새지 않는다.
        assert "슬롯 검증 (Node1)" not in reasoning
        assert "청크 전부 필요" not in reasoning
        # 그러나 검증 근거는 재현 핀(spec_driven.verify.slots[].rationale)에 남는다.
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        rationales = [s.get("rationale") for s in pin["verify"]["slots"]]
        assert "governing_clause 청크 전부 필요" in rationales
        assert "requirement_text 청크 전부 필요" in rationales


@pytest.mark.asyncio
async def test_verify_unwired_degrades_to_all_first_pass() -> None:
    # verify 미배선 → 슬롯 fallback(전량 necessary). 답변은 여전히 두 슬롯 생성.
    with tempfile.TemporaryDirectory() as tmp:
        runner = VariantRegistry.build(
            COMPOSER_PIPELINED_VARIANT_ID, _SPEC,
            _deps(Path(tmp), llm=_script(), with_verify=False))
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["verify"]["slots"][0]["method"] == "fallback"
        # fallback 이어도 1차 전량이 necessary → g1·r1 모두 컨텍스트.
        assert pin["verify"]["total_necessary"] == 2


def test_citation_allocator_assigns_global_and_dedups_shared_chunk() -> None:
    """SlotCitationAllocator — 슬롯별 전역 cite 배정 + 슬롯 간 공유 chunk 단일화(직접 단위)."""
    from app.application.agents.slot_pipeline import SlotCitationAllocator
    from app.domain.retrieval import RetrievedChunk

    builder = ContextBuilder(capture_mode="snippets")
    alloc = SlotCitationAllocator(builder)
    g1 = RetrievedChunk(chunk_id="g1", document_id="D1", score=0.9, snippet="b1")
    r1 = RetrievedChunk(chunk_id="r1", document_id="D2", score=0.8, snippet="b2")
    kw = dict(interaction_id="x", query_text="q", chat_history=(),
              conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
              entities={}, memory_refs=(), tool_result_refs=())

    p1 = alloc.build_slot_pack(chunks=[g1], **kw)
    p2 = alloc.build_slot_pack(chunks=[r1], **kw)
    # 슬롯별 다른 전역 번호.
    assert {c.citation_id for c in p1.pack.citation_candidates} == {"cite-0"}
    assert {c.citation_id for c in p2.pack.citation_candidates} == {"cite-1"}

    # 슬롯3 이 g1 을 다시 본다 → 새 번호 없이 기존 cite-0 재사용(References 중복 없음).
    p3 = alloc.build_slot_pack(chunks=[g1], **kw)
    assert {c.citation_id for c in p3.pack.citation_candidates} == {"cite-0"}
    assert p3.new_chunk_ids == []  # 새로 등장시킨 chunk 없음.
    # 전역 References 는 g1·r1 두 건만(중복 cite-0 후보 미생성).
    assert [c.citation_id for c in alloc.all_candidates] == ["cite-0", "cite-1"]
    assert alloc.all_chunk_ids == ["g1", "r1"]
