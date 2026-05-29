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


class _ClaimAwareLLM:
    """프롬프트로 호출 종류를 라우팅하는 controllable fake — generation 은 인용된
    답변을, decompose 프롬프트는 claim JSON 을, entailment 프롬프트는 verdict
    JSON 을 돌려준다. `entailment_status` 로 supported/contradicted/unsupported
    를 제어해 Node 15 분기를 fixture 로 검증(advisor: verdict 를 테스트)."""

    model_id = "claim-aware"

    def __init__(self, *, entailment_status: str = "supported",
                 answer: str = "i-SMR ECCS는 수동 냉각을 사용한다 [cite-0].") -> None:
        self._ent = entailment_status
        self._answer = answer

    async def generate(self, prompt, *, model_options=None, grammar=None):
        from app.ports.llm import LLMResult

        # 주의: citation-contract fragment 에 "분해" 가 있어 generation 프롬프트도
        # decompose 분기에 걸린다(테스트는 generated 본문 내용을 단언하지 않으므로
        # 무해). 프로덕션은 별도 호출이라 충돌 없음.
        if "분해" in prompt:  # Node 14 decompose
            text = json.dumps({"claims": [
                {"id": "cl-0", "text": "i-SMR ECCS는 수동 냉각을 사용한다", "cite_marker": "cite-0"}
            ]})
        elif "supported/contradicted" in prompt:  # Node 15 entailment
            text = json.dumps({"verdicts": [
                {"claim_id": "cl-0", "status": self._ent, "score": 0.95}
            ]})
        else:  # generation
            text = self._answer
        return LLMResult(text=text, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):
        from app.ports.llm import LLMTokenDelta

        r = await self.generate(prompt, model_options=model_options, grammar=grammar)
        yield LLMTokenDelta(content=r.text)
        yield LLMTokenDelta(finish_reason="stop", token_usage=dict(r.token_usage),
                            model_id=r.model_id)

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
    tmp: Path, *, with_contract: bool = True, retrieval_planner=None, retriever_tool=None,
    llm=None,
) -> tuple[HierarchicalCorrectiveRunner, FilesystemEventSink]:
    prompts = tmp / "prompts"
    build_prompts(prompts)
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    store = InMemorySessionMemoryStore()
    tools = {
        "retriever.search": retriever_tool or LocalRetrieverTool(),
        "document.resolve_citation": LocalDocumentResolverTool(),
        "memory.session_load": SessionLoadTool(store),
        "memory.session_update": SessionUpdateTool(store, ttl_days=90),
        "memory.approved_search": ApprovedSearchStubTool(),
        "verification.citation_check": LocalCitationCheckTool(),
        "verification.faithfulness_check": LocalFaithfulnessCheckTool(),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=sink)
    llm_router = LLMRouter(pool={"fake-echo": llm or FakeEchoLLM(model_id="fake-echo")}, default_id="fake-echo")
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
        # 실제 claim 경로(LLM decompose + entailment supported)로 PASS 까지 통과.
        runner, _ = _make_runner(Path(tmp), llm=_ClaimAwareLLM(entailment_status="supported"))
        req = AgentRequest(interaction_id="h1", query_text="APR1400 안전계통", session_id="s1")
        resp = await runner.run(req)

        assert resp.verification_status == "pass"
        assert resp.refusal_reason is None
        assert len(resp.citations) >= 1
        assert resp.evaluation is not None
        assert resp.evaluation.overall_decision == "pass"
        assert len(resp.claims) == 1 and resp.claims[0].status == "supported"

        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        line = next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        rec = json.loads(line)
        assert rec["agent_variant"] == HIERARCHICAL_CORRECTIVE_VARIANT_ID
        assert rec["retrieval_plan_hash"]
        assert rec["per_chunk_signals"][0]["decision"] == "pass"
        # generation + decompose(llm) + entailment = 3 LLM calls.
        assert rec["budget"]["llm_calls_used"] == 3
        assert rec["decompose_method"] == "llm"
        assert rec["entailment_model"] == "claim-aware"


class _TextChunkRetriever:
    """검색 chunk 에 *전체 본문*(text)이 채워진 현실 케이스. Node 9 window 가
    prompt 에 실제로 도달하는지(전역 capture_mode 와 무관)를 검증하기 위함 —
    LocalRetrieverTool 은 text=None 이라 이 경로를 노출하지 못한다(advisor)."""

    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context):
        from app.domain.retrieval import (
            RetrievedChunk,
            RetrieverSearchInput,
            RetrieverSearchOutput,
        )
        from app.domain.tools import ToolResult

        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)
        full = (
            "Lead junk sentence. "
            "i-SMR ECCS passive cooling design detail. "
            "Middle filler sentence. "
            "UNIQUEMARKER trailing tail sentence."
        )
        chunk = RetrievedChunk(
            chunk_id="t1", document_id="d", score=0.9, text=full, snippet=full[:40],
        )
        out = RetrieverSearchOutput(chunks=[chunk])
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="success",
            output=out.model_dump(mode="json"), latency_ms=0,
            input_hash="h", trace_id=context.trace_id,
        )


class _SequenceRetriever:
    """호출 순서대로 품질이 다른 chunk 를 반환 — recover 루프를 실제로 태우는
    instrument. mode: pass(질의어 전부)·weak(절반)·fail(무관). 마지막 mode 가
    이후 모든 호출에 반복 적용(clamp)."""

    name = "retriever.search"
    version = "v1"

    def __init__(self, modes: list[str]) -> None:
        self.modes = modes
        self.calls = 0

    async def invoke(self, tool_input, context):
        from app.domain.retrieval import (
            RetrievedChunk, RetrieverSearchInput, RetrieverSearchOutput,
        )
        from app.domain.tools import ToolResult

        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)
        mode = self.modes[min(self.calls, len(self.modes) - 1)]
        self.calls += 1
        ent = " ".join(v for vs in (tool_input.entities or {}).values() for v in vs if v)
        toks = tool_input.query_text.split()
        if mode == "pass":
            text = f"{tool_input.query_text} {ent}"
        elif mode == "weak":
            text = f"{' '.join(toks[: max(1, len(toks)//2)])} {ent}"
        else:  # fail — 질의·엔티티와 무관
            text = "completely unrelated boilerplate content"
        chunk = RetrievedChunk(chunk_id=f"c{self.calls}", document_id="d", score=0.8, snippet=text)
        out = RetrieverSearchOutput(chunks=[chunk])
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="success",
            output=out.model_dump(mode="json"), latency_ms=0, input_hash=f"h{self.calls}",
            trace_id=context.trace_id,
        )


@pytest.mark.asyncio
async def test_recover_weak_then_better_reaches_pass() -> None:
    """round1 FAIL → 복구 재검색이 PASS chunk 반환 → evaluation PASS, RecoverRound 기록."""
    fake = _SequenceRetriever(["fail", "pass"])
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), retriever_tool=fake, llm=_ClaimAwareLLM())
        req = AgentRequest(interaction_id="hr1", query_text="i-SMR ECCS passive design",
                           session_id="sr1")
        resp = await runner.run(req)
        assert resp.evaluation.overall_decision == "pass"
        assert len(resp.recover_rounds) >= 1
        assert resp.recover_rounds[-1].outcome_decision == "pass"
        assert resp.refusal_reason != "insufficient_evidence"
        # 복구 라운드의 chunk(c2)가 실제로 citation 까지 흘러갔는지 — 초기 c1 아님
        # (handoff 무결성, PR-6 window 교훈).
        assert any(c.chunk_id == "c2" for c in resp.citations)


@pytest.mark.asyncio
async def test_recover_exhausted_refuses_insufficient_evidence_and_terminates() -> None:
    """항상 FAIL → max_rounds 후 종료(무한루프 X) → INSUFFICIENT_EVIDENCE refuse.
    retriever 호출 = 최초 1 + recover 2 = 3 (종료 보장의 직접 증거)."""
    fake = _SequenceRetriever(["fail"])
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), retriever_tool=fake)
        req = AgentRequest(interaction_id="hr2", query_text="i-SMR ECCS passive design",
                           session_id="sr2")
        resp = await runner.run(req)
        assert resp.refusal_reason == "insufficient_evidence"
        assert resp.verification_status == "fail"
        assert fake.calls == 3  # 1 initial + 2 recover rounds, then stop
        # 평가 후 refusal 도 재현 데이터(recover_rounds·per_chunk_signals)를 event 에
        # 남겨야 "왜 거부했나"가 사후 추적 가능(defensibility, advisor).
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        assert len(rec["recover_rounds"]) == 2
        assert rec["per_chunk_signals"]
        assert rec["evaluator_policy_hash"]


@pytest.mark.asyncio
async def test_recover_weak_exhausted_proceeds_not_refused() -> None:
    """항상 WEAK → 복구 소진 후에도 refuse 하지 않고 진행(Node 15 claim gate 가
    backstop). INSUFFICIENT_EVIDENCE 아님."""
    fake = _SequenceRetriever(["weak"])
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), retriever_tool=fake)
        req = AgentRequest(interaction_id="hr3", query_text="i-SMR ECCS passive design",
                           session_id="sr3")
        resp = await runner.run(req)
        assert resp.refusal_reason != "insufficient_evidence"
        # WEAK 진행 → Node 15 가 판정(FakeEcho fallback → partial 등). FAIL refuse 아님.
        assert resp.verification_status in ("pass", "partial")


@pytest.mark.asyncio
async def test_contradicted_claim_refuses_with_verification_failed() -> None:
    """Node 15 가 contradicted 판정 → 답변 폐기 → VERIFICATION_FAILED refusal
    (답변을 버리는 안전-critical 분기)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), llm=_ClaimAwareLLM(entailment_status="contradicted"))
        req = AgentRequest(interaction_id="hc", query_text="i-SMR ECCS 설계", session_id="sc")
        resp = await runner.run(req)
        assert resp.verification_status == "fail"
        assert resp.refusal_reason == "verification_failed"
        assert resp.citations == ()
        assert resp.claims and resp.claims[0].status == "contradicted"


@pytest.mark.asyncio
async def test_unsupported_claim_yields_partial() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), llm=_ClaimAwareLLM(entailment_status="unsupported"))
        req = AgentRequest(interaction_id="hu", query_text="i-SMR ECCS 설계", session_id="su")
        resp = await runner.run(req)
        assert resp.verification_status == "partial"
        assert resp.refusal_reason == "partial_answer"


@pytest.mark.asyncio
async def test_v1_pass_carries_unverified_marker_in_response_object() -> None:
    """안전 계약: regulatory_enforced=False(v1) 인 PASS 는 응답 *객체* 에
    regulatory_grounding='unverified' + answer_text 마커를 달아 '완전 검증된
    답변'으로 오인되지 않게 한다(advisor #2 — event 뿐 아니라 응답 표면)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), llm=_ClaimAwareLLM(entailment_status="supported"))
        req = AgentRequest(interaction_id="hv", query_text="i-SMR ECCS 설계", session_id="sv")
        resp = await runner.run(req)
        assert resp.verification_status == "pass"
        assert resp.regulatory_grounding == "unverified"  # v1: 미강제
        assert "규제 근거 미검증" in resp.answer_text  # dumb client 도 보이게
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        assert rec["regulatory_grounding"] == "unverified"


@pytest.mark.asyncio
async def test_node9_window_reaches_prompt_not_full_text() -> None:
    """프로덕션 경로 검증: text 가 채워진 chunk 에서도 rendered prompt 에는 Node 9
    window 만 실리고 full text 의 제외 문장은 실리지 않는다(전역 capture_mode 가
    metadata 여도 v3.1 은 snippets 로 강제 렌더)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), retriever_tool=_TextChunkRetriever())
        req = AgentRequest(
            interaction_id="hw", query_text="i-SMR ECCS passive cooling design",
            session_id="sw",
        )
        await runner.run(req)
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "prompt_render_records").rglob("*.json"))
            .read_text(encoding="utf-8")
        )
        prompt = rec["rendered_prompt"]
        # window (best 문장 ± 1) 는 들어가고, window 밖 문장(UNIQUEMARKER)은 빠진다.
        assert "passive cooling design detail" in prompt
        assert "UNIQUEMARKER" not in prompt


@pytest.mark.asyncio
async def test_node9_records_evidence_pack_hash() -> None:
    """Node 9 가 실제 스니펫을 추출하면 event 에 evidence_pack_hash 가 실린다
    (PR-2 stub 에선 None 이었음)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp))
        req = AgentRequest(interaction_id="hs", query_text="i-SMR ECCS 설계", session_id="ss")
        await runner.run(req)
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        assert rec["evidence_pack_hash"]


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
        runner, _ = _make_runner(Path(tmp), retrieval_planner=planner, llm=_ClaimAwareLLM())
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


# ----------------------------------------------------------------------
# Phoenix(OTel) span emission — 무엇이 트레이스에 닿는가를 직접 검증한다.
# no-op 트레이서 아래선 set_attribute 가 silent no-op 이므로(그리고 OTel 은
# None/dict/mixed-seq attr 를 경고만 하고 *드롭*하므로) 실제 provider 를 꽂고
# 종료된 span 을 수집해 이름·attribute 존재를 단언해야 의미가 있다(advisor).
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def span_exporter():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # set_tracer_provider 는 최초 1회만 적용(이후 경고+무시). 유닛 스위트는 아무도
    # provider 를 설치하지 않으므로 여기서 이긴다. 모듈 import 시 캡처된 _TRACER
    # proxy 는 provider 설치 후 첫 span 생성에서 실제 tracer 로 lazily resolve 된다.
    trace.set_tracer_provider(provider)
    return exporter


def _spans_by_name(exporter) -> dict[str, list]:
    out: dict[str, list] = {}
    for sp in exporter.get_finished_spans():
        out.setdefault(sp.name, []).append(sp)
    return out


@pytest.mark.asyncio
async def test_phoenix_spans_emitted_on_pass_path(span_exporter) -> None:
    """PASS 경로: v3.1 의 Phase B/D 노드 + Phase D LLM 2회가 모두 span 으로
    Phoenix 에 닿는지 — 이름과 핵심 attribute 존재로 검증."""
    span_exporter.clear()
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), llm=_ClaimAwareLLM(entailment_status="supported"))
        req = AgentRequest(interaction_id="hp1", query_text="APR1400 안전계통", session_id="sp1")
        resp = await runner.run(req)
        assert resp.verification_status == "pass"

    by_name = _spans_by_name(span_exporter)

    # (1) Phase D LLM 호출 2개가 LLM span 으로 보인다(가장 심각했던 누락).
    for name in ("verify.claim_decompose", "verify.entailment", "llm.generation"):
        assert name in by_name, f"missing LLM span: {name}"
    ent = by_name["verify.entailment"][0]
    assert ent.attributes.get("openinference.span.kind") == "LLM"
    assert ent.attributes.get("llm.model_name") == "claim-aware"
    assert ent.attributes.get("entailment.ran") is True
    dec = by_name["verify.claim_decompose"][0]
    assert dec.attributes.get("decompose.method") == "llm"

    # (2) Phase B 결정론 노드들이 span 으로 그룹화된다.
    for name in ("agent.retrieval_execute", "agent.retrieval_evaluate",
                 "agent.evidence_snippet"):
        assert name in by_name, f"missing node span: {name}"
    ev = by_name["agent.retrieval_evaluate"][0]
    # 존재-단언이 dropped attr(None/wrong-type) 까지 잡는다.
    assert ev.attributes.get("evaluate.overall_decision") == "pass"
    assert "evaluate.num_pass" in ev.attributes
    assert "evaluate.policy_hash" in ev.attributes
    snip = by_name["agent.evidence_snippet"][0]
    assert "snippet.pack_hash" in snip.attributes  # None 이면 OTel 이 드롭 → 실패

    # (3) context_build RETRIEVER 타일에 chunk 가 실린다(v2 parity 회귀 복원).
    cb = by_name["agent.context_build"][0]
    assert cb.attributes.get("retrieval.documents.0.document.id")


@pytest.mark.asyncio
async def test_phoenix_recover_round_spans_distinguishable(span_exporter) -> None:
    """recover 라운드마다 별도 span 이 생기고 진단/결과 attribute 를 담아
    Phoenix 에서 어느 복구 라운드인지 구분된다(문제 #2 의 직접 검증)."""
    span_exporter.clear()
    fake = _SequenceRetriever(["fail", "pass"])
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), retriever_tool=fake, llm=_ClaimAwareLLM())
        req = AgentRequest(interaction_id="hp2", query_text="i-SMR ECCS passive design",
                           session_id="sp2")
        resp = await runner.run(req)
        assert len(resp.recover_rounds) >= 1

    rounds = _spans_by_name(span_exporter).get("agent.retrieval_recover", [])
    assert rounds, "recover round span not emitted"
    r0 = rounds[0]
    assert r0.attributes.get("recover.round") == 0
    assert r0.attributes.get("recover.diagnosis")  # str, non-empty
    assert r0.attributes.get("recover.outcome")  # 재평가 결과 기록
    # 재-dispatch 의 tool.retriever.search span 이 이 라운드 span 아래 nest 된다.
    tool_spans = [
        sp for sp in span_exporter.get_finished_spans()
        if sp.name.endswith("retriever.search")
        and sp.parent is not None
        and sp.parent.span_id == r0.context.span_id
    ]
    assert tool_spans, "recover round's retriever.search did not nest under round span"
