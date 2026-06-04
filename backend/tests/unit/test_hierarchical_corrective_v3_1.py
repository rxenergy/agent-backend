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
            "document.fetch_section": {"version": "v1", "adapter": "local", "timeout_ms": 3000, "retry": 0, "required": False},
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


class _FakeO4Classifier:
    """conf 0.5 로 O4/D2 를 주는 테스트용 분류기. P2 이후 None-shim 은 conf 0.0
    이라 scope/confidence 게이트 테스트는 분류기를 *명시 주입*해야 한다(plan W2)."""

    backend = "fake"
    policy_hash = "fake_o4"

    async def classify(self, query_text, chat_history=()):
        from app.domain.classification import ClassificationResult

        return ClassificationResult(
            scenario_object="O4", scenario_depth="D2", entities={},
            confidence=0.5, object_confidence=0.5, depth_confidence=0.5,
            classifier_backend=self.backend, classifier_policy_hash=self.policy_hash,
        )


def _make_runner(
    tmp: Path, *, with_contract: bool = True, retrieval_planner=None, retriever_tool=None,
    llm=None, claim_verification_enabled: bool = True,
    corpus_map=None, scope_tau_high: float = 0.6, scope_tau_low: float = 0.3,
    scope_min_token_count: int = 0, fetch_section_tool=None,
    context_token_budget: int = 0, section_merge_max_chunks: int = 50,
    classifier=None, active_cells_mode: str = "all", scenarios=None,
) -> tuple[HierarchicalCorrectiveRunner, FilesystemEventSink]:
    prompts = tmp / "prompts"
    build_prompts(prompts, scenarios=scenarios)
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    store = InMemorySessionMemoryStore()
    from app.adapters.tools.document_local import LocalDocumentFetchSectionTool
    tools = {
        "retriever.search": retriever_tool or LocalRetrieverTool(),
        "document.resolve_citation": LocalDocumentResolverTool(),
        "document.fetch_section": fetch_section_tool or LocalDocumentFetchSectionTool(),
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
        classifier=classifier,
        citation_contract_path=str(_CONTRACT) if with_contract else None,
        retrieval_planner=retrieval_planner,
        claim_verification_enabled=claim_verification_enabled,
        corpus_map=corpus_map,
        scope_tau_high=scope_tau_high,
        scope_tau_low=scope_tau_low,
        scope_min_token_count=scope_min_token_count,
        context_token_budget=context_token_budget,
        section_merge_max_chunks=section_merge_max_chunks,
        active_cells_mode=active_cells_mode,
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
        # 성공 경로에도 scope 가 기록된다(corpus_map 미주입 → default → off, 단 해시는 존재).
        assert rec["scope_mode"] == "off"
        assert rec["corpus_map_hash"]
        assert rec["per_chunk_signals"][0]["decision"] == "pass"
        # generation + decompose(llm) + entailment = 3 LLM calls.
        assert rec["budget"]["llm_calls_used"] == 3
        assert rec["decompose_method"] == "llm"
        assert rec["entailment_model"] == "claim-aware"
        # P1: 분류 정책 핀이 event 에 기록된다(원칙 5). 미주입이므로 hardcoded 핀.
        assert rec["classifier_policy_hash"]


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
async def test_weak_evaluation_injects_quality_advisory_into_prompt() -> None:
    """WEAK 평가 시 Node 12 가 검색 품질 advisory 를 *생성 전* 프롬프트에 싣는다 —
    답변이 '검색 근거가 왜 부족한지'를 설명하도록(생성-검증 결합 결함 보완)."""
    fake = _SequenceRetriever(["weak"])
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), retriever_tool=fake)
        req = AgentRequest(interaction_id="hwq", query_text="i-SMR ECCS passive design",
                           session_id="swq")
        resp = await runner.run(req)
        assert resp.evaluation.overall_decision == "weak"
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "prompt_render_records").rglob("*.json"))
            .read_text(encoding="utf-8")
        )
        prompt = rec["rendered_prompt"]
        assert "RETRIEVAL QUALITY ADVISORY" in prompt
        assert "WEAK" in prompt
        # 거부가 아니라 '한계를 밝힌 답변'을 요구하는 톤인지(proceed-on-WEAK 보존).
        assert "답변 거부가 아니라" in prompt


@pytest.mark.asyncio
async def test_pass_evaluation_omits_quality_advisory() -> None:
    """PASS 경로에는 품질 advisory 가 프롬프트에 실리지 않는다(WEAK 한정)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), llm=_ClaimAwareLLM(entailment_status="supported"))
        req = AgentRequest(interaction_id="hpq", query_text="i-SMR ECCS 설계", session_id="spq")
        resp = await runner.run(req)
        assert resp.evaluation.overall_decision == "pass"
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "prompt_render_records").rglob("*.json"))
            .read_text(encoding="utf-8")
        )
        assert "RETRIEVAL QUALITY ADVISORY" not in rec["rendered_prompt"]


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
async def test_claim_verification_disabled_skips_phase_d_and_passes_answer() -> None:
    """claim_verification_enabled=False 면 contradicted 를 낼 LLM 이라도 Phase D 를
    건너뛰어 답변을 폐기하지 않는다 — SKIPPED 로 통과, 생성 텍스트·인용 유지,
    decompose/entailment LLM 호출 미발생(budget 에 generation 1회만)."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), llm=_ClaimAwareLLM(entailment_status="contradicted"),
            claim_verification_enabled=False,
        )
        req = AgentRequest(interaction_id="hd", query_text="i-SMR ECCS 설계", session_id="sd")
        resp = await runner.run(req)
        assert resp.verification_status == "skipped"
        assert resp.refusal_reason is None
        assert len(resp.citations) >= 1
        assert resp.claims == ()
        rec = json.loads(
            next((Path(tmp) / "events" / "t" / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        assert rec["budget"]["llm_calls_used"] == 1  # generation 만
        assert rec["decompose_method"] is None
        assert rec["entailment_model"] is None


@pytest.mark.asyncio
async def test_v1_pass_carries_unverified_marker_in_response_object() -> None:
    """안전 계약: regulatory_enforced=False(v1) 인 PASS 는 구조화 필드
    regulatory_grounding='unverified' 로 '완전 검증된 답변' 오인을 막는다(응답 객체 +
    event). 표시 고지는 더 이상 answer_text 에 baking 하지 않고 API boundary
    (answer_renderer)가 content callout 으로 합성한다(decision A) — 따라서 응답 객체
    answer_text 는 깨끗한 LLM 본문이어야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), llm=_ClaimAwareLLM(entailment_status="supported"))
        req = AgentRequest(interaction_id="hv", query_text="i-SMR ECCS 설계", session_id="sv")
        resp = await runner.run(req)
        assert resp.verification_status == "pass"
        assert resp.regulatory_grounding == "unverified"  # v1: 미강제(단일 표현 소스)
        assert "규제 근거 미검증" not in resp.answer_text  # baking 제거 — boundary 가 표시
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


class _ScopeCapturingRetriever:
    """검색 호출마다 받은 scope(filters/target/min_token_count)를 기록하고,
    게이트를 FAIL 시키는 빈약 chunk 를 반환한다(질의어 무관 → s_lex≈0). recover
    루프를 강제로 태워 '복구 라운드에서 hard filter 가 해제되는가'(plan 결정 #2)
    와 refusal event 의 scope 기록을 함께 검증한다."""

    name = "retriever.search"
    version = "v1"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke(self, tool_input, context):
        from app.domain.retrieval import (
            RetrievedChunk,
            RetrieverSearchInput,
            RetrieverSearchOutput,
        )
        from app.domain.tools import ToolResult

        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)
        self.calls.append(
            {"filters": dict(tool_input.filters), "target": dict(tool_input.target),
             "min_token_count": tool_input.min_token_count}
        )
        # 질의어와 무관한 본문 → lexical/regulatory 신호 0 → 게이트 FAIL.
        chunk = RetrievedChunk(
            chunk_id="z1", document_id="d", score=0.05,
            text="zzz unrelated boilerplate zzz", snippet="zzz unrelated boilerplate zzz",
            token_count=4,
        )
        out = RetrieverSearchOutput(chunks=[chunk])
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="success",
            output=out.model_dump(mode="json"), latency_ms=0,
            input_hash="h", trace_id=context.trace_id,
        )


def _scope_corpus_map():
    from app.application.retrieval.corpus_map import CorpusMap

    # _FakeO4Classifier 주입 시 scenario_object=O4, confidence=0.5.
    # tau_high=0.4 로 0.5>=0.4 → filter mode. 룰은 scenario_object_in:[O4] 로 매칭.
    # (P2 이후 None-shim 은 conf 0.0 이라 분류기를 명시 주입해야 filter 발동.)
    return CorpusMap(
        topic_routing=[
            {"id": "t-o4", "when": {"scenario_object_in": ["O4"]},
             "scope": {"collection": ["SRP"]}}
        ],
        chunk_quality={"min_token_count": 0},  # floor 0 — recover 검증과 분리
    )


@pytest.mark.asyncio
async def test_scope_filter_applied_then_dropped_on_recover() -> None:
    fake = _ScopeCapturingRetriever()
    cm = _scope_corpus_map()
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), retriever_tool=fake, corpus_map=cm,
            scope_tau_high=0.4, scope_tau_low=0.3,
            classifier=_FakeO4Classifier(),
        )
        req = AgentRequest(interaction_id="sc1", query_text="APR1400 안전계통", session_id="ss1")
        resp = await runner.run(req)

        # 빈약 chunk → 복구 소진 후 INSUFFICIENT_EVIDENCE refusal.
        assert resp.refusal_reason == "insufficient_evidence"
        assert len(fake.calls) >= 2  # 최초 1 + recover N
        # 최초 검색엔 hard filter 적용(scope=filter).
        assert fake.calls[0]["filters"] == {"collection": ["SRP"]}
        # 복구 라운드는 filter 해제(recall 절벽 방어), boost(target)은 유지·비어있음.
        assert fake.calls[1]["filters"] == {}

        # refusal event 에도 scope 가 기록된다("scope 가 막다른 벽으로 좁혔나").
        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        rec = json.loads(
            next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        )
        assert rec["scope_mode"] == "filter"
        assert rec["corpus_map_hash"]


# ---- P1/P2 (Section auto-merge · multi-hop · budget) ----------------------
from app.domain.retrieval import (  # noqa: E402
    ChunkSignals,
    EvaluationResult,
    RetrievedChunk,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult  # noqa: E402
from app.ports.tool import ToolExecutionContext  # noqa: E402


class _FakeFetchSection:
    """document.fetch_section fake — 입력을 기록하고 구성된 형제/홉 chunk 반환."""

    name = "document.fetch_section"
    version = "v1"

    def __init__(self, chunks_by_key=None) -> None:
        # key=(source_id, section_key) → list[RetrievedChunk]
        self._by_key = chunks_by_key or {}
        self.calls: list[dict] = []

    async def invoke(self, tool_input, context):
        from app.domain.retrieval import DocumentFetchSectionInput

        if isinstance(tool_input, dict):
            tool_input = DocumentFetchSectionInput.model_validate(tool_input)
        self.calls.append({"source_id": tool_input.source_id,
                           "section_key": tool_input.section_key,
                           "match": tool_input.match})
        out = RetrieverSearchOutput(
            chunks=self._by_key.get((tool_input.source_id, tool_input.section_key), [])
        )
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="success",
            output=out.model_dump(mode="json"), latency_ms=0, input_hash="h",
            trace_id=context.trace_id,
        )


def _ctx_t() -> ToolExecutionContext:
    return ToolExecutionContext(interaction_id="i", trace_id="t", app_profile="local",
                               agent_variant=HIERARCHICAL_CORRECTIVE_VARIANT_ID)


@pytest.mark.asyncio
async def test_section_merge_expands_promoted_leaf_without_regate() -> None:
    sibs = [
        RetrievedChunk(chunk_id="S1_c0002", document_id="S1", score=0.4,
                       snippet="sibling two", token_count=20),
        RetrievedChunk(chunk_id="S1_c0001", document_id="S1", score=0.4,
                       snippet="sibling one", token_count=15),
    ]
    fake = _FakeFetchSection({("S1", "3.5.1 Sub"): sibs})
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), fetch_section_tool=fake)
        chunks = [
            RetrievedChunk(chunk_id="A", document_id="S1", source_id="S1", score=0.5,
                           snippet="A leaf window", token_count=5,
                           section_path=["3.5", "3.5.1 Sub"]),
            RetrievedChunk(chunk_id="B", document_id="S2", source_id="S2", score=0.5,
                           snippet="B leaf", token_count=5, section_path=["9.9 Other"]),
        ]
        evaluation = EvaluationResult(per_chunk=(
            ChunkSignals(chunk_id="A", decision="weak"),
            ChunkSignals(chunk_id="B", decision="pass"),
        ))
        out, stash, promoted = await runner._section_merge(
            chunks, evaluation, ctx=_ctx_t(), record=lambda r: None,
        )
        by_id = {c.chunk_id: c for c in out}
        # 승격된 A 는 형제(ordinal 순)로 확장, token_count 재합산. 인용 단위(chunk_id) 보존.
        assert by_id["A"].snippet == "sibling one\nsibling two"
        assert by_id["A"].token_count == 35
        # B(PASS)는 fetch 안 함, 그대로.
        assert by_id["B"].snippet == "B leaf"
        assert promoted == {"A"}
        assert stash["A"] == ("A leaf window", 5)   # demote 복원용 스태시
        # term 매칭(자기 full 경로 원소)으로만 호출(B 미호출).
        assert fake.calls == [{"source_id": "S1", "section_key": "3.5.1 Sub", "match": "term"}]


@pytest.mark.asyncio
async def test_section_merge_noop_when_all_pass() -> None:
    fake = _FakeFetchSection()
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), fetch_section_tool=fake)
        chunks = [RetrievedChunk(chunk_id="A", document_id="S1", source_id="S1",
                                 score=0.9, section_path=["1"])]
        evaluation = EvaluationResult(per_chunk=(ChunkSignals(chunk_id="A", decision="pass"),))
        out, stash, promoted = await runner._section_merge(
            chunks, evaluation, ctx=_ctx_t(), record=lambda r: None)
        assert promoted == set() and stash == {} and out == chunks
        assert fake.calls == []   # PASS 면 fetch 안 함


@pytest.mark.asyncio
async def test_multi_hop_follows_section_ref() -> None:
    hop = RetrievedChunk(chunk_id="S1_c0099", document_id="S1", score=0.3,
                         snippet="referenced section body", token_count=10)
    fake = _FakeFetchSection({("S1", "3.2"): [hop]})
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), fetch_section_tool=fake)
        chunks = [RetrievedChunk(chunk_id="A", document_id="S1", source_id="S1",
                                 score=0.8, snippet="as defined in §3.2 of the rule")]
        edges: list = []
        hop_chunks = await runner._multi_hop(
            chunks, ctx=_ctx_t(), record=lambda r: None, edges_out=edges)
        assert [c.chunk_id for c in hop_chunks] == ["S1_c0099"]
        assert len(edges) == 1
        assert edges[0].from_chunk_id == "A" and edges[0].target_id == "3.2"
        assert fake.calls[0]["match"] == "prefix"   # 번호 prefix 매칭


@pytest.mark.asyncio
async def test_context_budget_demotes_then_drops_then_reorders() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), context_token_budget=10)
        chunks = [
            RetrievedChunk(chunk_id="c0", document_id="d", score=0.9,
                           snippet="merged section big", token_count=8),
            RetrievedChunk(chunk_id="c1", document_id="d", score=0.5,
                           snippet="x", token_count=8),
            RetrievedChunk(chunk_id="c2", document_id="d", score=0.2,
                           snippet="y", token_count=8),
        ]
        stash = {"c0": ("leaf", 2)}   # c0 는 승격된 것 → leaf 복원 시 2 토큰
        log: list = []
        out = runner._apply_context_budget(chunks, stash, budget_log=log)
        ids = {c.chunk_id for c in out}
        # demote c0(8→2): 총 24→18, drop tail c2(8): 18→10 ≤ budget. c2 제거.
        assert "c2" not in ids
        assert any(a.startswith("demote:c0") for a in log)
        assert any(a.startswith("drop:c2") for a in log)
        # c0 는 leaf 로 강등(snippet/token 복원).
        c0 = next(c for c in out if c.chunk_id == "c0")
        assert c0.snippet == "leaf" and c0.token_count == 2


@pytest.mark.asyncio
async def test_context_budget_off_by_default_is_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp))   # context_token_budget=0
        assert runner._context_token_budget == 0


class _RefChunkRetriever:
    """검색 chunk 가 §참조를 포함하는 현실 케이스 — Node 8 다홉 end-to-end 검증.
    질의어를 snippet 에 실어 게이트 PASS(진행)시키고, source_id + §3.2 를 둔다."""

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
        snip = f"{tool_input.query_text} acceptance criteria; see §3.2 of the rule [cite-0]."
        chunk = RetrievedChunk(
            chunk_id="D1_c0007", document_id="D1", source_id="D1", score=0.9,
            snippet=snip, section_path=["3.5 Missile", "3.5.1 Turbine"], token_count=12,
        )
        out = RetrieverSearchOutput(chunks=[chunk])
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="success",
            output=out.model_dump(mode="json"), latency_ms=0, input_hash="h",
            trace_id=context.trace_id,
        )


@pytest.mark.asyncio
async def test_run_records_hops_from_section_ref_end_to_end() -> None:
    hop = RetrievedChunk(chunk_id="D1_c0030", document_id="D1", score=0.4,
                         snippet="section 3.2 body text", token_count=10)
    fake_fetch = _FakeFetchSection({("D1", "3.2"): [hop]})
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), retriever_tool=_RefChunkRetriever(),
            fetch_section_tool=fake_fetch, claim_verification_enabled=False,
        )
        req = AgentRequest(interaction_id="mh1", query_text="APR1400 safety",
                           session_id="mhs1")
        resp = await runner.run(req)
        # 다홉이 §3.2 를 추적했고(답변은 거부되지 않음) event 에 hops 가 기록된다.
        assert resp.refusal_reason is None
        assert fake_fetch.calls and fake_fetch.calls[0]["match"] == "prefix"

        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        rec = json.loads(
            next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        )
        assert rec["hops"], "hop edge not recorded in event"
        assert rec["hops"][0]["ref_kind"] == "section"
        assert rec["hops"][0]["target_id"] == "3.2"


class _EntailmentCapturingLLM(_ClaimAwareLLM):
    """_ClaimAwareLLM + entailment 프롬프트를 캡처해 검증기가 *무슨 근거*를 받았는지
    단언할 수 있게 한다(Finding 1: Section 병합 후 검증기가 leaf window 가 아니라
    섹션 본문을 봐야 한다)."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.entail_prompts: list[str] = []

    async def generate(self, prompt, *, model_options=None, grammar=None):
        if "supported/contradicted" in prompt:
            self.entail_prompts.append(prompt)
        return await super().generate(prompt, model_options=model_options, grammar=grammar)


class _TwoChunkRetriever:
    """chunk0=FAIL(질의 무관, 승격 대상) + chunk1=PASS(질의 일치, overall 진행).
    chunk0 에 source_id+section_path 를 둬 Section 병합 대상이 되게 한다."""

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
        chunks = [
            RetrievedChunk(chunk_id="W_c0001", document_id="W", source_id="W", score=0.9,
                           snippet="unrelated boilerplate fragment [cite-0]",
                           section_path=["3.5", "3.5.1 Sub"], token_count=6),
            RetrievedChunk(chunk_id="P_c0001", document_id="P", source_id="P", score=0.8,
                           snippet=f"{tool_input.query_text} acceptance criteria",
                           section_path=["1 Intro"], token_count=6),
        ]
        out = RetrieverSearchOutput(chunks=chunks)
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="success",
            output=out.model_dump(mode="json"), latency_ms=0, input_hash="h",
            trace_id=context.trace_id,
        )


@pytest.mark.asyncio
async def test_section_merge_evidence_reaches_verifier_with_claims_on() -> None:
    """Finding 1 회귀 가드: Section 병합이 발동하고 claim 검증이 켜진 production 경로.
    검증기(entailment)가 받는 근거가 leaf window 가 아니라 *병합된 섹션 본문*이어야
    한다(아니면 형제-근거 claim 이 거짓 unsupported)."""
    sibs = [
        RetrievedChunk(chunk_id="W_c0001", document_id="W", score=0.4,
                       snippet="SIBLINGMARKER passive cooling detail", token_count=20),
        RetrievedChunk(chunk_id="W_c0002", document_id="W", score=0.4,
                       snippet="SIBLINGMARKER injection criteria", token_count=18),
    ]
    fetch = _FakeFetchSection({("W", "3.5.1 Sub"): sibs})
    llm = _EntailmentCapturingLLM(entailment_status="supported")
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), retriever_tool=_TwoChunkRetriever(), fetch_section_tool=fetch,
            llm=llm,  # claims ON (기본)
        )
        req = AgentRequest(interaction_id="sm-e2e", query_text="reactor safety",
                           session_id="sm-e2e-s")
        resp = await runner.run(req)

        # 병합된 cite-0(=W_c0001)의 근거가 섹션 본문(SIBLINGMARKER)으로 검증기에 전달됨.
        assert llm.entail_prompts, "entailment did not run"
        assert "SIBLINGMARKER" in llm.entail_prompts[0], (
            "verifier got stale leaf window, not merged section text (Finding 1)"
        )
        assert resp.refusal_reason is None  # 형제-근거 claim 이 거짓 거부되지 않음

        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        rec = json.loads(
            next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        )
        # 실제 병합이 일어났으므로 정책 해시·promote 기록(Finding 2: 병합 시에만).
        assert rec["section_merge_policy_hash"]
        promoted = [s for s in rec["per_chunk_signals"] if s.get("promote")]
        assert promoted and promoted[0]["chunk_id"] == "W_c0001"


class _ScopeClassifier:
    """scope_tier/intent 를 명시 산출하는 테스트용 분류기(LLM 분류기 대역).
    Node 2 scope 라우팅 분기(T3 메타 / T4 OUT_OF_SCOPE / T1 정상)를 검증한다."""

    backend = "llm"
    policy_hash = "fake_scope"

    def __init__(self, *, scope_tier: str, intent: str = "definition",
                 scenario_object: str = "O4", scenario_depth: str = "D2") -> None:
        self._tier = scope_tier
        self._intent = intent
        self._so = scenario_object
        self._sd = scenario_depth

    async def classify(self, query_text, chat_history=()):
        from app.domain.classification import ClassificationResult

        return ClassificationResult(
            scenario_object=self._so, scenario_depth=self._sd, entities={},
            confidence=0.8, object_confidence=0.8, depth_confidence=0.8,
            classifier_backend=self.backend, classifier_policy_hash=self.policy_hash,
            intent=self._intent, scope_tier=self._tier,
        )


def _read_event(tmp: Path) -> dict:
    events_root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


@pytest.mark.asyncio
async def test_scope_tier_t3_meta_short_circuits_without_retrieval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), classifier=_ScopeClassifier(scope_tier="T3", intent="meta"),
        )
        req = AgentRequest(interaction_id="m1", query_text="너는 뭘 할 수 있어?", session_id="s1")
        resp = await runner.run(req)

        # 메타 응답: 거부 아님, 인용 없음, 검색 미수행.
        assert resp.refusal_reason is None
        assert resp.citations == ()
        assert resp.scope_tier == "T3"
        assert resp.classifier_intent == "meta"
        assert "SMR" in resp.answer_text
        rec = _read_event(Path(tmp))
        assert rec["scope_tier"] == "T3"
        assert rec["classifier_intent"] == "meta"
        # 검색 도구 호출이 없어야 한다(retriever.search 미기록).
        assert all(tc["name"] != "retriever.search" for tc in rec["tool_calls"])


@pytest.mark.asyncio
async def test_scope_tier_t4_refuses_out_of_scope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), classifier=_ScopeClassifier(scope_tier="T4", intent="unknown"),
        )
        req = AgentRequest(interaction_id="o1", query_text="오늘 날씨 어때?", session_id="s1")
        resp = await runner.run(req)

        assert resp.refusal_reason == "out_of_scope"
        assert resp.citations == ()
        assert resp.scope_tier == "T4"
        rec = _read_event(Path(tmp))
        assert rec["refusal_reason"] == "out_of_scope"
        assert rec["error_code"] == "out_of_scope"
        assert rec["scope_tier"] == "T4"


@pytest.mark.asyncio
async def test_scope_tier_t1_proceeds_to_normal_path_and_records_signals() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), llm=_ClaimAwareLLM(entailment_status="supported"),
            classifier=_ScopeClassifier(scope_tier="T1", intent="causal"),
        )
        req = AgentRequest(interaction_id="t1", query_text="APR1400 안전계통", session_id="s1")
        resp = await runner.run(req)

        # 정상 검색·답변 경로 — scope_tier/intent 가 event 에 기록된다.
        assert resp.scope_tier == "T1"
        assert resp.classifier_intent == "causal"
        rec = _read_event(Path(tmp))
        assert rec["scope_tier"] == "T1"
        assert rec["classifier_intent"] == "causal"
        assert any(tc["name"] == "retriever.search" for tc in rec["tool_calls"])


@pytest.mark.asyncio
async def test_inactive_cell_downgrades_instead_of_unsupported_refusal() -> None:
    # is_active 정교화(taxonomy plan W-S2): top_priority 모드에서 비핵심 셀(O1×D1,
    # TOP_PRIORITY 밖)이라도 scope_tier=T1/T2 면 UNSUPPORTED_SCENARIO 로 일률 거부하지
    # 않고 정상 경로로 강등 진행한다. (이전 동작=거부 → 회귀 가드.)
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), llm=_ClaimAwareLLM(entailment_status="supported"),
            active_cells_mode="top_priority",
            scenarios=[("O1", "D2"), ("O4", "D2"), ("O1", "D1")],
            classifier=_ScopeClassifier(
                scope_tier="T1", intent="enumerate",
                scenario_object="O1", scenario_depth="D1",
            ),
        )
        req = AgentRequest(interaction_id="dg1", query_text="FSAR 구성", session_id="s1")
        resp = await runner.run(req)

        # 비활성 셀이지만 거부되지 않고 검색·답변 경로로 진행.
        assert resp.refusal_reason != "unsupported_scenario"
        rec = _read_event(Path(tmp))
        assert rec["error_code"] != "inactive_cell"
        assert any(tc["name"] == "retriever.search" for tc in rec["tool_calls"])


@pytest.mark.asyncio
async def test_live_intent_threads_into_plan_and_information_need_recorded() -> None:
    # Node 3 모델 기반 정보 요구(문서 §4) — 두 가지를 end-to-end 로 검증:
    #  (1) Node 1 live intent(comparison)가 query_plan.intents 로 흘러 Node 4
    #      planner 의 intent-keyed 룰을 *실제로* 발동(이전엔 버려져 항상 default).
    #  (2) 모델 산출 정보 요구(method/슬롯/intents)가 event 에 기록된다(기록 전용).
    from app.application.retrieval.planner import RetrievalPlanner

    planner = RetrievalPlanner(
        default_strategies=["hybrid"],
        rules=[{
            "id": "comparison_multi_strategy",
            "when": {"intents_any": ["comparison"]},
            "strategies": ["hybrid", "bm25"],
        }],
        fusion="rrf", rrf_k=60,
    )
    # 분류기 entities={} 이므로 planner 의 entity_hash 도 {} 기준 — 기대 해시를
    # 동일 입력으로 직접 재계산해 runner 의 plan 이 intent 를 소비했음을 증명.
    plan_with = planner.plan(
        scenario_object="O4", scenario_depth="D2", entities={}, intents=("comparison",))
    plan_without = planner.plan(
        scenario_object="O4", scenario_depth="D2", entities={}, intents=())
    assert plan_with.rule_id == "comparison_multi_strategy"
    assert plan_without.rule_id == "default"
    assert plan_with.plan_hash != plan_without.plan_hash

    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp),
            llm=_ClaimAwareLLM(entailment_status="supported"),
            retrieval_planner=planner,
            classifier=_ScopeClassifier(
                scope_tier="T1", intent="comparison",
                scenario_object="O4", scenario_depth="D2"),
        )
        req = AgentRequest(interaction_id="n3", query_text="APR1400 vs i-SMR 비교",
                           session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason is None

        rec = _read_event(Path(tmp))
        qu = rec["query_understanding"]
        # (1) intent 가 plan.intents 로 threaded → planner 가 comparison 룰 채택.
        assert qu["intents"] == ["comparison"]
        assert rec["retrieval_plan_hash"] == plan_with.plan_hash
        # (2) 모델 정보 요구 기록 — fake LLM 은 JSON 미반환이라 fallback(prior 슬롯).
        assert qu["instantiation_method"] == "fallback"
        assert qu["required_slots"] == ["comparison_dimension", "requirement_text"]
        assert qu["information_need_hash"]
        # fallback 은 LLM 산출이 아니므로 decompose_prompt_hash 부재(invariant).
        assert qu["decompose_prompt_hash"] is None


class _NeedAwareLLM:
    """Node 3 information_need 프롬프트엔 *요구 JSON*(version_constraint·sub_questions
    포함)을, 그 외엔 decompose/entailment/generation 을 돌려주는 controllable fake.
    Node 3 의 *LLM 성공 경로*(production 경로)를 runner end-to-end 로 노출한다 —
    fallback 만 타던 기존 테스트의 사각을 메운다(advisor)."""

    model_id = "need-aware"

    def __init__(self, *, version_constraint: str = "2024-06-01") -> None:
        self._vc = version_constraint

    async def generate(self, prompt, *, model_options=None, grammar=None):
        from app.ports.llm import LLMResult

        # 순서 주의: Node 3 프롬프트엔 "정보 요구"와 "분해"가 둘 다 있으므로
        # "정보 요구"를 *먼저* 검사. entailment 는 "분해"보다 먼저.
        if "정보 요구" in prompt:  # Node 3 information_need
            text = json.dumps({
                "required_slots": [
                    {"name": "governing_clause", "required": True},
                    {"name": "effective_version", "required": True},
                ],
                "sub_questions": ["ECCS 요건은?", "발효 개정일은?"],
                "version_constraint": self._vc,
                "multi_intent": True,
            })
        elif "supported/contradicted" in prompt:  # Node 15 entailment
            text = json.dumps({"verdicts": [
                {"claim_id": "cl-0", "status": "supported", "score": 0.95}]})
        elif "분해" in prompt:  # Node 14 decompose
            text = json.dumps({"claims": [
                {"id": "cl-0", "text": "i-SMR ECCS는 수동 냉각을 사용한다",
                 "cite_marker": "cite-0"}]})
        else:  # generation
            text = "i-SMR ECCS는 수동 냉각을 사용한다 [cite-0]."
        return LLMResult(text=text, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):
        from app.ports.llm import LLMTokenDelta

        r = await self.generate(prompt, model_options=model_options, grammar=grammar)
        yield LLMTokenDelta(content=r.text)
        yield LLMTokenDelta(finish_reason="stop", token_usage=dict(r.token_usage),
                            model_id=r.model_id)


@pytest.mark.asyncio
async def test_node3_llm_success_path_threads_subquestions_and_version() -> None:
    # Node 3 LLM 성공 경로(production) — fallback 이 아닌 *모델 산출* 이 sub_questions·
    # version_constraint 를 채우고, 그게 다운스트림에서 spurious refusal 없이 흘러
    # event 에 실린다. version_constraint 가 채워져도 v1 은 effective_on=null →
    # version_conflict=None → FAIL 없음(검증된 무해성).
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), llm=_NeedAwareLLM(version_constraint="2024-06-01"),
            classifier=_ScopeClassifier(scope_tier="T1", intent="compliance"),
        )
        req = AgentRequest(interaction_id="n3llm",
                           query_text="i-SMR ECCS 준수 요건과 발효일은?", session_id="s1")
        resp = await runner.run(req)

        # version_constraint 가 채워졌어도 거부되지 않고 완주(v1 inert 경험적 확인).
        assert resp.refusal_reason is None
        rec = _read_event(Path(tmp))
        qu = rec["query_understanding"]
        assert qu["instantiation_method"] == "llm"
        assert qu["sub_question_count"] == 2
        assert qu["version_constraint"] == "2024-06-01"
        assert qu["multi_intent"] is True
        assert qu["required_slots"] == ["governing_clause", "effective_version"]
        # LLM 성공이므로 decompose_prompt_hash 존재(invariant: 존재=LLM 호출).
        assert qu["decompose_prompt_hash"]
        # Node 3 need + generation + decompose + entailment = 4 LLM calls.
        assert rec["budget"]["llm_calls_used"] == 4
