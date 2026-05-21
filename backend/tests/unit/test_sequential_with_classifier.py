from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.session_store.in_memory import InMemorySessionMemoryStore
from app.adapters.llm.fake import FakeEchoLLM
from app.adapters.tools.document_local import LocalDocumentResolverTool
from app.adapters.tools.memory_approved_stub import ApprovedSearchStubTool
from app.adapters.tools.memory_session_local import SessionLoadTool, SessionUpdateTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.verification_local import (
    LocalCitationCheckTool,
    LocalFaithfulnessCheckTool,
)
from app.application.agents.llm_router import LLMRouter
from app.application.agents.sequential_tool_routed_v2 import SequentialToolRoutedRunner
from app.application.classification.rule import RuleClassifier
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.memory.summarizer import ConversationSummarizer
from app.application.prompting.local_source import LocalPromptSource
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import PromptResolver
from app.domain.agents import VariantSpec
from tests.unit._prompts_fixture import build_prompts as _shared_build_prompts

_TEST_SPEC = VariantSpec(variant_id="sequential_tool_routed_v2")
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.classification import ClassificationResult
from app.domain.interaction import AgentRequest


def _build_prompts(root: Path) -> None:
    _shared_build_prompts(root, scenarios=[("O1", "D2"), ("O4", "D2")])


def _build_tool_registry(root: Path) -> Path:
    body = {
        "tools": {
            k: {"version": "v1", "adapter": "local", "timeout_ms": 5000, "retry": 0, "required": req}
            for k, req in {
                "retriever.search": True,
                "document.resolve_citation": True,
                "memory.session_load": False,
                "memory.session_update": False,
                "memory.approved_search": False,
                "verification.citation_check": True,
                "verification.faithfulness_check": True,
            }.items()
        }
    }
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


class _FixedClassifier:
    backend = "fixed"

    def __init__(self, obj: str, dep: str, conf: float = 0.9) -> None:
        self._obj = obj
        self._dep = dep
        self._conf = conf

    async def classify(self, query_text, chat_history=()):
        return ClassificationResult(
            scenario_object=self._obj,
            scenario_depth=self._dep,
            entities={"vendors": ["NuScale"]} if self._obj == "O1" else {},
            confidence=self._conf,
            object_confidence=self._conf,
            depth_confidence=self._conf,
            classifier_backend=self.backend,
        )


def _make_runner(
    tmp: Path,
    *,
    classifier=None,
    cit_thr: float = 0.9,
    faith_thr: float = 0.85,
    retry: bool = False,
    summarizer: ConversationSummarizer | None = None,
):
    prompts = tmp / "prompts"
    _build_prompts(prompts)
    tools_yaml = _build_tool_registry(tmp)

    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    session_store = InMemorySessionMemoryStore()

    registry = ToolRegistry.from_yaml(tools_yaml)
    tools = {
        "retriever.search": LocalRetrieverTool(),
        "document.resolve_citation": LocalDocumentResolverTool(),
        "memory.session_load": SessionLoadTool(session_store),
        "memory.session_update": SessionUpdateTool(session_store, ttl_days=90),
        "memory.approved_search": ApprovedSearchStubTool(),
        "verification.citation_check": LocalCitationCheckTool(),
        "verification.faithfulness_check": LocalFaithfulnessCheckTool(),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(
        pool={"fake-echo": FakeEchoLLM(model_id="fake-echo")},
        default_id="fake-echo",
    )
    runner = SequentialToolRoutedRunner(
        spec=_TEST_SPEC,
        llm_router=llm_router,
        tool_executor=executor,
        prompt_resolver=PromptResolver(LocalPromptSource(prompts)),
        prompt_renderer=PromptRenderer(),
        context_builder=ContextBuilder(capture_mode="full"),
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        classifier=classifier,
        classification_threshold=0.5 if classifier else 0.0,
        verification_citation_threshold=cit_thr,
        verification_faithfulness_threshold=faith_thr,
        verification_retry_on_fail=retry,
        summarizer=summarizer,
    )
    return runner, sink, session_store


@pytest.mark.asyncio
async def test_classifier_drives_scenario_and_entities() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, sink, _ = _make_runner(Path(tmp), classifier=_FixedClassifier("O1", "D2", 0.9))
        req = AgentRequest(interaction_id="i1", query_text="NuScale 설계", session_id="s1")
        resp = await runner.run(req)

        assert resp.scenario_object == "O1"
        assert resp.scenario_depth == "D2"
        assert resp.classification_confidence == 0.9
        assert resp.entities.get("vendors") == ["NuScale"]

        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        files = list(events_root.rglob("*.jsonl"))
        rec = json.loads(files[0].read_text().splitlines()[0])
        assert rec["scenario_object"] == "O1"
        assert rec["classification_confidence"] == 0.9


@pytest.mark.asyncio
async def test_unresolved_profile_refuses_unknown_scenario() -> None:
    """Fail-fast: an active (O, D) with no registered prompt profile must refuse
    before the LLM is called — no silent fallback prompt (spec §6)."""
    with tempfile.TemporaryDirectory() as tmp:
        # Fixture only registers (O1,D2) and (O4,D2). O2/D3 is active but unmapped.
        runner, _, _ = _make_runner(
            Path(tmp), classifier=_FixedClassifier("O2", "D3", 0.9)
        )
        req = AgentRequest(interaction_id="i-miss", query_text="질문", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason == "unknown_scenario"
        assert resp.verification_status == "skipped"
        assert resp.citations == ()

        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        files = list(events_root.rglob("*.jsonl"))
        rec = json.loads(files[0].read_text().splitlines()[0])
        assert rec["error_code"] == "prompt_profile_not_found"


@pytest.mark.asyncio
async def test_event_carries_prompt_composition_hash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _ = _make_runner(
            Path(tmp), classifier=_FixedClassifier("O1", "D2", 0.9)
        )
        req = AgentRequest(interaction_id="i-hash", query_text="질문", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason is None

        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        files = list(events_root.rglob("*.jsonl"))
        rec = json.loads(files[0].read_text().splitlines()[0])
        assert rec["prompt_composition_hash"]
        assert rec["rendered_prompt_hash"]
        assert rec["prompt_composition_hash"] != rec["rendered_prompt_hash"]
        assert rec["prompt_fragment_versions"]["system"] == "v1"
        assert rec["prompt_source"] == "local"


@pytest.mark.asyncio
async def test_low_confidence_triggers_clarification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _ = _make_runner(
            Path(tmp), classifier=_FixedClassifier("O1", "D2", 0.1)
        )
        req = AgentRequest(interaction_id="i-low", query_text="???", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason == "clarification_required"
        assert resp.verification_status == "skipped"
        assert resp.citations == ()


@pytest.mark.asyncio
async def test_verification_threshold_partial() -> None:
    # citation passes (1.0) but faithfulness threshold higher than achievable → partial
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _ = _make_runner(
            Path(tmp),
            classifier=_FixedClassifier("O4", "D2", 0.9),
            faith_thr=0.99,   # local stub gives 0.9 with 3 chunks → < 0.99 → partial
            retry=False,
        )
        req = AgentRequest(interaction_id="i-part", query_text="질문", session_id="s1")
        resp = await runner.run(req)
        # 0.9 >= 0.99*0.5=0.495 → partial (not full fail)
        assert resp.verification_status == "partial"
        assert resp.refusal_reason == "partial_answer"
        assert len(resp.citations) >= 1
