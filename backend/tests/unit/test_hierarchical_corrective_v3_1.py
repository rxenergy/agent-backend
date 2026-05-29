from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from tests.unit._prompts_fixture import build_prompts
from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeEchoLLM
from app.adapters.session_store.in_memory import InMemorySessionMemoryStore
from app.adapters.tools.document_local import LocalDocumentResolverTool
from app.adapters.tools.memory_approved_stub import ApprovedSearchStubTool
from app.adapters.tools.memory_session_local import SessionLoadTool, SessionUpdateTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.verification_local import (
    LocalCitationCheckTool,
    LocalFaithfulnessCheckTool,
)
from app.application.agents.hierarchical_corrective_v3_1 import (
    HIERARCHICAL_CORRECTIVE_VARIANT_ID,
    HierarchicalCorrectiveRunner,
)
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import VariantRegistry
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.local_source import LocalPromptSource
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import PromptResolver
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest

_SPEC = VariantSpec(variant_id=HIERARCHICAL_CORRECTIVE_VARIANT_ID)

_CONTRACT = Path(__file__).resolve().parents[3] / "prompts" / "system" / "citation_contract_v1.md"


def _tool_registry_yaml(root: Path) -> Path:
    body = {
        "tools": {
            "retriever.search": {"version": "v1", "adapter": "local", "timeout_ms": 5000, "retry": 1, "required": True},
            "document.resolve_citation": {"version": "v1", "adapter": "local", "timeout_ms": 2000, "retry": 0, "required": True},
            "memory.session_load": {"version": "v1", "adapter": "postgres", "timeout_ms": 1000, "retry": 0, "required": False},
            "memory.session_update": {"version": "v1", "adapter": "postgres", "timeout_ms": 1000, "retry": 0, "required": False},
            "memory.approved_search": {"version": "v1", "adapter": "postgres_pgvector", "timeout_ms": 1000, "retry": 0, "required": False},
            "verification.citation_check": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": True},
            "verification.faithfulness_check": {"version": "v1", "adapter": "local", "timeout_ms": 3000, "retry": 0, "required": True},
        }
    }
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _make_runner(
    tmp: Path, *, with_contract: bool = True, retrieval_planner=None,
) -> tuple[HierarchicalCorrectiveRunner, FilesystemEventSink]:
    prompts = tmp / "prompts"
    build_prompts(prompts)
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    store = InMemorySessionMemoryStore()
    tools = {
        "retriever.search": LocalRetrieverTool(),
        "document.resolve_citation": LocalDocumentResolverTool(),
        "memory.session_load": SessionLoadTool(store),
        "memory.session_update": SessionUpdateTool(store, ttl_days=90),
        "memory.approved_search": ApprovedSearchStubTool(),
        "verification.citation_check": LocalCitationCheckTool(),
        "verification.faithfulness_check": LocalFaithfulnessCheckTool(),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"fake-echo": FakeEchoLLM(model_id="fake-echo")}, default_id="fake-echo")
    runner = HierarchicalCorrectiveRunner(
        spec=_SPEC,
        llm_router=llm_router,
        tool_executor=executor,
        prompt_resolver=PromptResolver(LocalPromptSource(prompts)),
        prompt_renderer=PromptRenderer(),
        context_builder=ContextBuilder(capture_mode="full"),
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        citation_contract_path=str(_CONTRACT) if with_contract else None,
        retrieval_planner=retrieval_planner,
    )
    return runner, sink


@pytest.mark.asyncio
async def test_variant_is_registered() -> None:
    assert HIERARCHICAL_CORRECTIVE_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_full_workflow_runs_and_records_v31_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp))
        req = AgentRequest(interaction_id="h1", query_text="APR1400 안전계통", session_id="s1")
        resp = await runner.run(req)

        assert resp.verification_status == "pass"
        assert resp.refusal_reason is None
        assert len(resp.citations) >= 1
        # v3.1 response carries an evaluation summary.
        assert resp.evaluation is not None
        assert resp.evaluation.overall_decision == "pass"

        # Event must carry the v3.1 reproducibility fields, asdict-serialized.
        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        line = next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        rec = json.loads(line)
        assert rec["agent_variant"] == HIERARCHICAL_CORRECTIVE_VARIANT_ID
        assert rec["retrieval_plan_hash"]
        assert rec["per_chunk_signals"], "evaluator signals must be recorded"
        assert rec["per_chunk_signals"][0]["decision"] == "pass"
        assert rec["budget"]["llm_calls_used"] == 1
        assert rec["budget"]["total_llm_call_budget"] == 8
        assert rec["query_understanding"]["multi_intent"] is False


@pytest.mark.asyncio
async def test_event_records_regulatory_enforced_false_on_v1() -> None:
    """Node 6 — 기본(v1) 경로는 regulatory hard gate 미강제. event 에
    regulatory_enforced=false + 실제 신호값(s_lex 등)이 기록돼, v1 PASS 가
    '규제 검증된 PASS'로 오인되지 않게 한다(advisor #2)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp))  # regulatory_hard_gates_enforced 기본 False
        req = AgentRequest(interaction_id="hr", query_text="i-SMR ECCS 설계", session_id="sr")
        resp = await runner.run(req)
        assert resp.evaluation is not None
        assert resp.evaluation.regulatory_enforced is False

        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        assert rec["regulatory_enforced"] is False
        assert rec["evaluator_policy_hash"]
        # per_chunk_signals 는 stub 상수가 아니라 실제 계산된 신호값을 담는다.
        sig = rec["per_chunk_signals"][0]
        assert "s_lex" in sig and "entity_coverage" in sig and "hard_gates_passed" in sig


@pytest.mark.asyncio
async def test_multi_strategy_plan_fans_out_and_records_each_call() -> None:
    """With a 2-strategy plan, Node 5 must invoke retriever.search once per
    strategy and record each as a separate tool_call (strategies_executed)."""
    from app.application.retrieval.planner import RetrievalPlanner

    planner = RetrievalPlanner(default_strategies=["hybrid", "bm25"], rules=[], rrf_k=60)
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), retrieval_planner=planner)
        req = AgentRequest(interaction_id="hm", query_text="APR1400 비교", session_id="sm")
        resp = await runner.run(req)
        assert resp.refusal_reason is None

        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        retr_calls = [tc for tc in rec["tool_calls"] if tc["name"] == "retriever.search"]
        assert len(retr_calls) == 2, "both strategies must be recorded as tool_calls"
        # The two calls have distinct input_hash (strategy is in the payload).
        assert len({tc["input_hash"] for tc in retr_calls}) == 2


@pytest.mark.asyncio
async def test_run_stream_emits_v31_nodes_and_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp))
        req = AgentRequest(interaction_id="h2", query_text="질의", session_id="s2")

        names: list[str] = []
        kinds: list[str] = []
        final = None
        async for ev in runner.run_stream(req):
            kinds.append(ev.kind)
            if ev.name:
                names.append(ev.name)
            if ev.kind == "final":
                final = ev.payload["response"]

        assert final is not None
        assert kinds[-1] == "final"
        # New Phase B/D node steps are surfaced for the Thinking trace.
        for node in (
            "query_understanding", "retrieval_plan", "retrieval_execute",
            "retrieval_evaluate", "claim_verify",
        ):
            assert node in names, f"missing step: {node}"


@pytest.mark.asyncio
async def test_events_survive_openai_compat_translation_layer() -> None:
    """The new node steps + `status="skipped"` must pass through the SSE
    translation layer (`thinking_renderer.render` + `_event_to_smr`) without
    raising — this is the path `make smoke` exercises. Unknown step names
    render to no thinking line but still ride as `smr_agent.event` frames;
    "skipped" is never switched on as an error."""
    from app.api.openai_compat import _event_to_smr
    from app.api.thinking_renderer import render as render_thinking

    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp))
        req = AgentRequest(interaction_id="h4", query_text="질의", session_id="s4")

        saw_skipped = False
        async for ev in runner.run_stream(req):
            # Must not raise for any event kind/name/status combination.
            lines = render_thinking(ev, content_mode="metadata", max_items=5)
            assert isinstance(lines, list)
            if ev.kind in ("step", "tool"):
                smr = _event_to_smr(ev)
                assert smr["event"]["name"] == ev.name
                assert smr["event"]["status"] == ev.status
                if ev.status == "skipped":
                    saw_skipped = True
        assert saw_skipped, "expected at least one skipped node (recover/hop/decompose)"


@pytest.mark.asyncio
async def test_citation_contract_sha_recorded_in_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), with_contract=True)
        req = AgentRequest(interaction_id="h5", query_text="질의", session_id="s5")
        await runner.run(req)
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        assert rec["query_understanding"]["citation_contract_sha"]


@pytest.mark.asyncio
async def test_citation_contract_changes_prompt_hash() -> None:
    """With the citation contract preamble, the rendered prompt differs from
    the no-contract render — proving the contract actually reaches the prompt."""
    with tempfile.TemporaryDirectory() as tmp:
        runner_with, _ = _make_runner(Path(tmp), with_contract=True)
        req = AgentRequest(interaction_id="h3", query_text="질의", session_id="s3")
        async for _ in runner_with.run_stream(req):
            pass
        rec_with = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )

    with tempfile.TemporaryDirectory() as tmp:
        runner_without, _ = _make_runner(Path(tmp), with_contract=False)
        req = AgentRequest(interaction_id="h3", query_text="질의", session_id="s3")
        async for _ in runner_without.run_stream(req):
            pass
        rec_without = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )

    assert rec_with["rendered_prompt_hash"] != rec_without["rendered_prompt_hash"]
