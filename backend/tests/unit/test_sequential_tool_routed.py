from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.session_store.in_memory import InMemorySessionMemoryStore
from app.adapters.llm.fake import FakeEchoLLM
from app.adapters.tools.artifact_event import WriteEventTool
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
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import PromptResolver
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.interaction import AgentRequest


def _build_prompts(root: Path) -> None:
    (root / "system").mkdir(parents=True)
    (root / "object").mkdir()
    (root / "depth").mkdir()
    (root / "cell").mkdir()
    (root / "schemas").mkdir()
    (root / "system" / "sys_v1.md").write_text("SYS")
    (root / "object" / "o4_v1.md").write_text("O4")
    (root / "depth" / "d2_v1.md").write_text("D2")
    (root / "cell" / "o4_d2_v1.md").write_text("CELL")
    (root / "schemas" / "answer_v1.json").write_text("{}")
    registry = {
        "prompt_profiles": {
            "o4_d2_v1": {
                "version": "v1",
                "scenario_object": "O4",
                "scenario_depth": "D2",
                "system": "system/sys_v1.md",
                "object": "object/o4_v1.md",
                "depth": "depth/d2_v1.md",
                "cell": "cell/o4_d2_v1.md",
                "output_schema": "schemas/answer_v1.json",
                "model_options": {"temperature": 0.1},
            }
        }
    }
    (root / "registry.yaml").write_text(yaml.safe_dump(registry))


def _build_tool_registry(root: Path) -> Path:
    body = {
        "tools": {
            "retriever.search": {"version": "v1", "adapter": "local", "timeout_ms": 5000, "retry": 1, "required": True},
            "document.resolve_citation": {"version": "v1", "adapter": "local", "timeout_ms": 2000, "retry": 0, "required": True},
            "memory.session_load": {"version": "v1", "adapter": "postgres", "timeout_ms": 1000, "retry": 0, "required": False},
            "memory.session_update": {"version": "v1", "adapter": "postgres", "timeout_ms": 1000, "retry": 0, "required": False},
            "memory.approved_search": {"version": "v1", "adapter": "postgres_pgvector", "timeout_ms": 1000, "retry": 0, "required": False},
            "verification.citation_check": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": True},
            "verification.faithfulness_check": {"version": "v1", "adapter": "local", "timeout_ms": 3000, "retry": 0, "required": True},
            "artifact.write_event": {"version": "v1", "adapter": "object_store", "timeout_ms": 1000, "retry": 1, "required": True},
        }
    }
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


class _UnavailableLLM:
    """LLMPort double that always raises LLMUnavailableError."""

    async def generate(self, prompt, *, model_options=None):
        from app.ports.llm import LLMUnavailableError
        raise LLMUnavailableError("upstream 503: overloaded")


class _CountingLLM:
    """LLMPort double that records call count — used to verify retry path."""

    def __init__(self) -> None:
        self.calls = 0
        self.model_id = "counting"

    async def generate(self, prompt, *, model_options=None):
        from app.ports.llm import LLMResult
        self.calls += 1
        return LLMResult(
            text="답변 [c1]",
            token_usage={"prompt_tokens": 1, "completion_tokens": 1},
            model_id=self.model_id,
        )


def _make_runner(
    tmp: Path,
    *,
    llm=None,
    verification_retry_on_fail: bool = False,
    verification_citation_threshold: float = 0.5,
    verification_faithfulness_threshold: float = 0.5,
) -> tuple[SequentialToolRoutedRunner, FilesystemEventSink, InMemorySessionMemoryStore]:
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
        "artifact.write_event": WriteEventTool(),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)

    llm_instance = llm or FakeEchoLLM(model_id="fake-echo")
    llm_router = LLMRouter(pool={"fake-echo": llm_instance}, default_id="fake-echo")
    runner = SequentialToolRoutedRunner(
        llm_router=llm_router,
        tool_executor=executor,
        prompt_resolver=PromptResolver(str(prompts)),
        prompt_renderer=PromptRenderer(prompt_dir=prompts),
        context_builder=ContextBuilder(capture_mode="full"),
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        verification_citation_threshold=verification_citation_threshold,
        verification_faithfulness_threshold=verification_faithfulness_threshold,
        verification_retry_on_fail=verification_retry_on_fail,
    )
    return runner, sink, session_store


@pytest.mark.asyncio
async def test_full_workflow_records_tool_calls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, sink, _ = _make_runner(Path(tmp))
        req = AgentRequest(interaction_id="i1", query_text="APR1400 안전계통", session_id="s1")
        resp = await runner.run(req)

        assert resp.refusal_reason is None
        assert len(resp.citations) >= 1

        # event jsonl contains 7 tool_calls (session_load, retrieve, approved, doc_resolve,
        # citation_check, faithfulness, session_update, write_event = 8 total)
        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        files = list(events_root.rglob("*.jsonl"))
        assert files
        import json
        line = files[0].read_text(encoding="utf-8").strip().splitlines()[0]
        rec = json.loads(line)
        assert rec["agent_variant"] == "sequential_tool_routed_v2"
        assert len(rec["tool_calls"]) == 8
        assert rec["context_hash"]
        assert rec["rendered_prompt_hash"]


@pytest.mark.asyncio
async def test_llm_unavailable_returns_refusal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _ = _make_runner(Path(tmp), llm=_UnavailableLLM())
        req = AgentRequest(
            interaction_id="i-unavail", query_text="질문", session_id="s1"
        )
        resp = await runner.run(req)
        assert resp.refusal_reason == "llm_unavailable"
        assert resp.verification_status == "fail"
        assert resp.citations == ()


@pytest.mark.asyncio
async def test_verification_retry_invokes_llm_again() -> None:
    """Retry path must use the resolved LLM, not a non-existent attribute.
    Regression for `self._current_llm` AttributeError when
    verification_retry_on_fail=True."""
    with tempfile.TemporaryDirectory() as tmp:
        llm = _CountingLLM()
        runner, _, _ = _make_runner(
            Path(tmp),
            llm=llm,
            verification_retry_on_fail=True,
            verification_citation_threshold=1.1,  # unattainable → forces retry
        )
        req = AgentRequest(interaction_id="i-retry", query_text="질문", session_id="s1")
        resp = await runner.run(req)
        # Either FAIL or PARTIAL — never crashes with AttributeError.
        assert resp.verification_status in ("fail", "partial")
        assert llm.calls == 2  # initial + 1 retry


@pytest.mark.asyncio
async def test_multi_turn_persists_session_memory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, session_store = _make_runner(Path(tmp))
        await runner.run(AgentRequest(interaction_id="i1", query_text="질문1", session_id="s1"))
        mem = await session_store.get("s1")
        assert mem is not None
        assert mem.session_id == "s1"
        assert len(mem.recent_turns) >= 1
