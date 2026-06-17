"""spec_driven_v2 — 실 vLLM 통합 테스트(Phase 1: 단일노드 클론).

Node1·Node2 둘 다 실제 vLLM(gemma-4-awq)이 서빙 중인 환경을 전제로 한다(fake 미사용 —
사용자 결정). 환경변수로 엔드포인트를 받아 실제 `HttpLLM` 을 만들고, 미설정 시 모듈 전체
skip 한다(opt-in, 다른 integration 테스트와 동형).

  SPEC_DRIVEN_V2_NODE1_ENDPOINT — Node1 vLLM OpenAI-compat 엔드포인트
                                  (예: http://192.168.100.10:8001/v1)
  SPEC_DRIVEN_V2_NODE1_MODEL    — Node1 모델 id (예: gemma-4-26b-a4b-it)

Phase 1 은 v2 가 v1 처럼 단일노드로 동작함을 검증한다(변형 등록·선택·프롬프트 profile_id
분리). Node2 검증·per-slot 파이프라인은 Phase 2~4 에서 추가되며 그때 테스트가 확장된다.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.http import HttpLLM
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.application.agents.spec_driven_v2 import (
    SPEC_DRIVEN_V2_VARIANT_ID,
    SpecDrivenV2Runner,
)
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

pytestmark = pytest.mark.integration

_SPEC = VariantSpec(variant_id=SPEC_DRIVEN_V2_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"


def _node1_endpoint() -> str | None:
    return os.environ.get("SPEC_DRIVEN_V2_NODE1_ENDPOINT")


@pytest.fixture(scope="session")
def node1_llm() -> HttpLLM:
    ep = _node1_endpoint()
    if not ep:
        pytest.skip(
            "SPEC_DRIVEN_V2_NODE1_ENDPOINT not set; spec_driven_v2 integration skipped"
        )
    model = os.environ.get("SPEC_DRIVEN_V2_NODE1_MODEL", "gemma-4-26b-a4b-it")
    return HttpLLM(provider="openai_compat", endpoint=ep, model=model,
                   timeout_s=120.0, max_attempts=2)


def _tool_registry_yaml(root: Path) -> Path:
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
    }}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _deps(tmp: Path, *, llm) -> AgentDeps:
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    tools = {
        "retrieval.search": RetrievalSearchTool(
            retriever=LocalRetrieverTool(), reranker=IdentityReranker()
        ),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"node1": llm}, default_id="node1")
    return AgentDeps(
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        llm_router=llm_router,
        utility_llm=llm,  # Node1 — N1/N2/N4 를 같은 vLLM 으로.
        tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_v2_answer_spec_source=SpecDrivenAnswerSpecV2Source(_REPO_PROMPTS),
        spec_driven_v2_query_source=SpecDrivenQueryV2Source(_REPO_PROMPTS),
        spec_driven_v2_generation_source=SpecDrivenGenerationV2Source(_REPO_PROMPTS),
        spec_driven_v2_triage_source=SpecDrivenTriageV2Source(_REPO_PROMPTS),
        spec_driven_v2_general_source=SpecDrivenGeneralV2Source(_REPO_PROMPTS),
        tunables={
            "citation_contract_path": str(_CONTRACT),
            "retriever_top_k": 3,
            "spec_driven_max_queries": 6,
        },
    )


def _build(tmp: Path, llm) -> SpecDrivenV2Runner:
    runner = VariantRegistry.build(SPEC_DRIVEN_V2_VARIANT_ID, _SPEC, _deps(tmp, llm=llm))
    assert isinstance(runner, SpecDrivenV2Runner)
    return runner


def _req() -> AgentRequest:
    return AgentRequest(interaction_id="v2-it-1",
                        query_text="10 CFR 50.46 ECCS 요건은?", model="node1")


def test_variant_is_registered() -> None:
    # 부팅 의존성 없는 순수 등록 검증 — 엔드포인트 미설정이어도 의미 있음.
    assert SPEC_DRIVEN_V2_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_end_to_end_grounded_real_vllm(node1_llm: HttpLLM) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), node1_llm)
        resp = await runner.run(_req())
        # 실 vLLM 응답이라 본문 텍스트는 단언하지 않는다(비결정). 거부 아님 + 경로 핀만 검증.
        assert resp.refusal_reason is None
        assert resp.answer_text.strip() != ""
        # 프롬프트 profile_id 가 v2 로 분리됐는지(재현 핀, 원칙 5).
        import json as _json

        root = Path(tmp) / "events" / "t" / "interaction_events"
        line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        event = _json.loads(line)
        assert event["prompt_profile_id"] == "spec_driven_generation_v2"
        assert event["agent_variant"] == "spec_driven_v2"


def _node2_endpoint() -> str | None:
    return os.environ.get("SPEC_DRIVEN_V2_NODE2_ENDPOINT")


def _deps_two_node(tmp: Path, *, node1, node2, verify_source) -> AgentDeps:
    """Node1(생성/쿼리/외부참조) + Node2(검증 도구) 둘 다 배선한 2-노드 deps."""
    from app.adapters.slot_verifier_llm import SlotVerifierLlm
    from app.adapters.tools.retrieval_verify_slot import RetrievalVerifySlotTool

    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
        "retrieval.verify_slot": {"version": "v1", "adapter": "vllm_verify",
                                  "timeout_ms": 60000, "retry": 0, "required": False},
    }}
    p = tmp / "tool_registry_2node.yaml"
    p.write_text(yaml.safe_dump(body))
    registry = ToolRegistry.from_yaml(p)
    tools = {
        "retrieval.search": RetrievalSearchTool(
            retriever=LocalRetrieverTool(), reranker=IdentityReranker()
        ),
        "retrieval.verify_slot": RetrievalVerifySlotTool(
            slot_verifier=SlotVerifierLlm(llm=node2, source=verify_source),
            max_concurrency=2,
        ),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"node1": node1}, default_id="node1")
    return AgentDeps(
        recorder=recorder, event_sink=sink, app_profile="local",
        llm_router=llm_router, utility_llm=node1, tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_v2_answer_spec_source=SpecDrivenAnswerSpecV2Source(_REPO_PROMPTS),
        spec_driven_v2_query_source=SpecDrivenQueryV2Source(_REPO_PROMPTS),
        spec_driven_v2_generation_source=SpecDrivenGenerationV2Source(_REPO_PROMPTS),
        spec_driven_v2_triage_source=SpecDrivenTriageV2Source(_REPO_PROMPTS),
        spec_driven_v2_general_source=SpecDrivenGeneralV2Source(_REPO_PROMPTS),
        tunables={"citation_contract_path": str(_CONTRACT), "retriever_top_k": 3,
                  "spec_driven_max_queries": 6, "spec_driven_v2_verify_concurrency": 2},
    )


@pytest.mark.asyncio
async def test_two_node_end_to_end_verify_pin(node1_llm: HttpLLM) -> None:
    # Node1(생성) + Node2(검증) 둘 다 실 vLLM. verify 가 슬롯별로 Node2 에서 돌고 재현 핀에
    # verify/node1_llm_id 가 남는지 검증(본문은 비결정이라 단언 안 함).
    ep2 = _node2_endpoint()
    if not ep2:
        pytest.skip("SPEC_DRIVEN_V2_NODE2_ENDPOINT not set; 2-node test skipped")
    from app.application.prompting.spec_driven_source import SpecDrivenVerifySource

    model2 = os.environ.get("SPEC_DRIVEN_V2_NODE2_MODEL", "gemma-4-26b-a4b-it")
    node2 = HttpLLM(provider="openai_compat", endpoint=ep2, model=model2,
                    timeout_s=120.0, max_attempts=2)
    verify_source = SpecDrivenVerifySource(_REPO_PROMPTS)
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps_two_node(Path(tmp), node1=node1_llm, node2=node2,
                              verify_source=verify_source)
        runner = VariantRegistry.build(SPEC_DRIVEN_V2_VARIANT_ID, _SPEC, deps)
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        import json as _json

        root = Path(tmp) / "events" / "t" / "interaction_events"
        line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        pin = _json.loads(line)["query_understanding"]["spec_driven"]
        assert pin["verify"]["node2"] is True
        assert pin["verify"]["num_slots"] >= 1
        assert pin["node1_llm_id"] == "node1"
        # 최종 컨텍스트는 necessary 기준(1차 전량 보존 아님) — 핀 키 존재 검증.
        assert "necessary_kept" in pin["retrieval"]
