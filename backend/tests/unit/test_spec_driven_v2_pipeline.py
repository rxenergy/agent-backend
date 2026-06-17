"""spec_driven_v2 per-slot 파이프라인 단위 테스트(fake 도구 — 컨테이너/실 LLM 불요).

병합·trim 로직은 결정론이라 fake 포트로 잠근다(실 vLLM 통합 테스트는 integration/ 에서
별도). 검증 포인트:
  - N4 컨텍스트 = Node2 가 고른 necessary ∪ 2차(멀티홉) 결과 — 1차 *전량 보존 아님*
    (1차이나 not-necessary 청크는 drop).
  - verify 도구 미배선(ToolUnknown) → 슬롯 전량 necessary 보존(단일노드 degrade).
  - node1/node2 재현 핀 + per-slot verify 핀 존재.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.application.agents.spec_driven_v2 import SPEC_DRIVEN_V2_VARIANT_ID
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.spec_driven_source import (
    SpecDrivenAnswerSpecV2Source,
    SpecDrivenGeneralV2Source,
    SpecDrivenGenerationV2Source,
    SpecDrivenQueryV2Source,
    SpecDrivenTriageV2Source,
)
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest
from app.domain.tools import ToolResult
from app.ports.llm import LLMResult, LLMTokenDelta

_SPEC = VariantSpec(variant_id=SPEC_DRIVEN_V2_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"

_TRIAGE_RETRIEVAL = json.dumps(
    {"rationale": "특정 조문", "references_specifics": True, "route": "retrieval"}
)
# 단일 슬롯 — 1차에서 여러 청크가 잡히지만 Node2 가 일부만 necessary 로 고른다.
_SPEC_JSON = json.dumps({
    "intent": "compliance",
    "explicit_references": ["10 CFR 50.46"],
    "governing_normative_class": "binding",
    "required_slots": [
        {"name": "governing_clause",
         "keywords": ["10 CFR 50.46", "ECCS"], "required": True},
    ],
    "answer_structure": "지배조문",
})
_QUERIES_JSON = json.dumps({
    "queries": [
        {"slot_name": "governing_clause",
         "query_text": "ECCS acceptance criteria", "collection": "10CFR"},
    ]
})
_ANSWER = "ECCS 요건 [cite-1]."


class _ScriptLLM:
    def __init__(self, gen_texts: list[str]) -> None:
        self._gen = list(gen_texts)
        self._i = 0
        self.model_id = "fake"

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        t = self._gen[min(self._i, len(self._gen) - 1)]
        self._i += 1
        return LLMResult(text=t, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None,
                              grammar=None) -> AsyncIterator[LLMTokenDelta]:
        yield LLMTokenDelta(content=_ANSWER, model_id=self.model_id,
                            token_usage={"prompt_tokens": 1, "completion_tokens": 5})

    async def generate_with_tools(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


class _MultiChunkRetriever:
    """1차 검색은 3건(c1>c2>c3), source_id 필터가 걸린 2차는 1건(hop1) 반환."""

    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        is_second = bool((getattr(tool_input, "filters", None) or {}).get("source_id")
                         if not isinstance(tool_input, dict)
                         else (tool_input.get("filters") or {}).get("source_id"))
        if is_second:
            # 2차 검색은 2건 — Stage4 재검증이 hop1 만 필요로 골라 hop2 는 drop 되는지 검증.
            chunks = [{"chunk_id": "hop1", "document_id": "EXT", "score": 0.7,
                       "snippet": "external ref body", "source_id": "ML_T"},
                      {"chunk_id": "hop2", "document_id": "EXT", "score": 0.6,
                       "snippet": "irrelevant external body", "source_id": "ML_T"}]
        else:
            chunks = [
                {"chunk_id": "c1", "document_id": "D1", "score": 0.9,
                 "snippet": "ECCS governing clause body", "source_id": "S1"},
                {"chunk_id": "c2", "document_id": "D1", "score": 0.8,
                 "snippet": "tangential body", "source_id": "S1"},
                {"chunk_id": "c3", "document_id": "D1", "score": 0.7,
                 "snippet": "noise toc", "source_id": "S1"},
            ]
        return ToolResult(tool_name="retriever.search", tool_version="v1",
                          status="success", output={"chunks": chunks},
                          latency_ms=0, input_hash="x", trace_id="")


class _FakeVerifyTool:
    """Node2 — 입력 청크 id 에 따라 분기(Stage1 1차 / Stage4 2차 모두 처리).
    - 1차(c1/c2/c3): c1 만 necessary, c1 은 멀티홉도 필요(외부 참조). c2/c3 drop.
    - 2차(hop1/hop2): Stage4 재검증 — hop1 만 necessary(hop2 drop)."""

    name = "retrieval.verify_slot"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        chunks = (tool_input.get("chunks") if isinstance(tool_input, dict)
                  else getattr(tool_input, "chunks", [])) or []
        ids = {(c.get("chunk_id") if isinstance(c, dict) else c.chunk_id) for c in chunks}
        if "hop1" in ids or "hop2" in ids:  # Stage4 2차 재검증
            out = {"necessary_chunk_ids": ["hop1"], "multihop_chunk_ids": [],
                   "rationale": "hop1 relevant, hop2 not", "method": "llm"}
        else:  # Stage1 1차 검증
            out = {"necessary_chunk_ids": ["c1"], "multihop_chunk_ids": ["c1"],
                   "rationale": "c1 only", "method": "llm"}
        return ToolResult(
            tool_name="retrieval.verify_slot", tool_version="v1", status="success",
            output=out, latency_ms=0, input_hash="x", output_hash="y", trace_id="",
        )


class _FakeFollowUpTool:
    name = "retrieval.follow_up"
    version = "v2"

    async def invoke(self, tool_input, context) -> ToolResult:
        return ToolResult(
            tool_name="retrieval.follow_up", tool_version="v2", status="success",
            output={"follow_up_queries": [
                {"query_text": "external limit", "target_source_ids": ["ML_T"],
                 "intent": "limit"},
            ]},
            latency_ms=0, input_hash="x", output_hash="y", trace_id="",
        )


def _tool_registry_yaml(root: Path, *, with_verify: bool) -> Path:
    tools: dict[str, Any] = {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
        "retrieval.follow_up": {"version": "v2", "adapter": "vllm_ref",
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
    retriever = _MultiChunkRetriever()
    tools: dict[str, Any] = {
        "retrieval.search": retriever,
        "retrieval.follow_up": _FakeFollowUpTool(),
    }
    if with_verify:
        tools["retrieval.verify_slot"] = _FakeVerifyTool()
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"fake": llm}, default_id="fake")
    return AgentDeps(
        recorder=recorder, event_sink=sink, app_profile="local",
        llm_router=llm_router, utility_llm=llm, tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_v2_answer_spec_source=SpecDrivenAnswerSpecV2Source(_REPO_PROMPTS),
        spec_driven_v2_query_source=SpecDrivenQueryV2Source(_REPO_PROMPTS),
        spec_driven_v2_generation_source=SpecDrivenGenerationV2Source(_REPO_PROMPTS),
        spec_driven_v2_triage_source=SpecDrivenTriageV2Source(_REPO_PROMPTS),
        spec_driven_v2_general_source=SpecDrivenGeneralV2Source(_REPO_PROMPTS),
        tunables={"citation_contract_path": str(_CONTRACT), "retriever_top_k": 3,
                  "spec_driven_max_queries": 6, "spec_driven_max_context_chunks": 10,
                  "spec_driven_v2_verify_concurrency": 3},
    )


def _script() -> _ScriptLLM:
    return _ScriptLLM([_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _ANSWER])


def _event(tmp: Path) -> dict:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def _req() -> AgentRequest:
    return AgentRequest(interaction_id="v2u1",
                        query_text="10 CFR 50.46 ECCS 요건은?", model="fake")


@pytest.mark.asyncio
async def test_n4_context_is_necessary_plus_second_pass_not_all_first_pass() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = VariantRegistry.build(
            SPEC_DRIVEN_V2_VARIANT_ID, _SPEC, _deps(Path(tmp), llm=_script(), with_verify=True)
        )
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        event = _event(tmp)
        ret_ids = set(event["retrieved_chunk_ids"])
        # c1(necessary) + hop1(Stage4 통과 2차) 만. c2/c3(1차 not-necessary) + hop2(Stage4
        # 재검증 drop)는 제외 — "검색 후 무조건 Node2 relevance".
        assert "c1" in ret_ids
        assert "hop1" in ret_ids
        assert "c2" not in ret_ids and "c3" not in ret_ids
        assert "hop2" not in ret_ids
        pin = event["query_understanding"]["spec_driven"]
        assert pin["verify"]["node2"] is True
        assert pin["verify"]["total_necessary"] == 1
        assert pin["verify"]["total_multihop"] == 1
        # Stage 4 — 2차 2건 검증 입력 중 1건만 통과.
        assert pin["verify"]["second_pass_total"] == 2
        assert pin["verify"]["second_necessary_total"] == 1
        assert pin["retrieval"]["necessary_kept"] == 1
        assert pin["retrieval"]["first_pass_total"] == 3
        assert pin["node1_llm_id"] == "fake"
        # per-slot verify 핀 — 1차 + 2차 재검증 메타.
        slots = pin["verify"]["slots"]
        assert slots[0]["slot"] == "governing_clause"
        assert slots[0]["method"] == "llm"
        assert slots[0]["num_second_pass"] == 2
        assert slots[0]["num_second_necessary"] == 1
        assert slots[0]["second_method"] == "llm"


@pytest.mark.asyncio
async def test_node2_verify_rationale_surfaces_in_thinking() -> None:
    # UI thinking — run_stream 의 reasoning 이벤트에 **슬롯 검증 (Node2)** 블록 + Node2
    # 판정 근거(1차 rationale + 2차 rationale2)가 실린다(B2: rationale 까지).
    with tempfile.TemporaryDirectory() as tmp:
        runner = VariantRegistry.build(
            SPEC_DRIVEN_V2_VARIANT_ID, _SPEC, _deps(Path(tmp), llm=_script(), with_verify=True)
        )
        parts: list[str] = []
        async for ev in runner.run_stream(_req()):
            if ev.kind == "reasoning":
                parts.append(ev.payload.get("content", ""))
        reasoning = "".join(parts)
        assert "**슬롯 검증 (Node2)**" in reasoning
        assert "c1 only" in reasoning                    # 1차 검증 근거
        assert "hop1 relevant, hop2 not" in reasoning    # 2차 재검증 근거(Stage 4)


@pytest.mark.asyncio
async def test_verify_unwired_degrades_to_all_first_pass() -> None:
    # verify 도구 미배선 → ToolUnknown → 슬롯 fallback(전량 necessary). 1차 전량 보존.
    with tempfile.TemporaryDirectory() as tmp:
        runner = VariantRegistry.build(
            SPEC_DRIVEN_V2_VARIANT_ID, _SPEC, _deps(Path(tmp), llm=_script(), with_verify=False)
        )
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        event = _event(tmp)
        ret_ids = set(event["retrieved_chunk_ids"])
        # fallback → c1/c2/c3 모두 necessary. 멀티홉 없음 → 2차 검색 안 함.
        assert {"c1", "c2", "c3"} <= ret_ids
        pin = event["query_understanding"]["spec_driven"]
        assert pin["verify"]["slots"][0]["method"] == "fallback"
        assert pin["verify"]["total_multihop"] == 0
