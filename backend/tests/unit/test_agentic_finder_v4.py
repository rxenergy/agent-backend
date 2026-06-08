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
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.memory_approved_stub import ApprovedSearchStubTool
from app.adapters.tools.memory_session_local import SessionLoadTool, SessionUpdateTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retrieval_scope import RetrievalScopeTool
from app.adapters.tools.terminology_canonicalize import TerminologyCanonicalizeTool
from app.application.terminology.vocab import TerminologyVocab
from app.adapters.tools.submit_verdict import SubmitVerdictTool
from app.application.agents.agentic_finder_v4 import (
    AGENTIC_FINDER_VARIANT_ID,
    AgenticFinderRunner,
)
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import VariantRegistry
from app.application.prompting.answer_spec_source import AnswerSpecPromptSource
from app.application.prompting.query_translate_source import QueryTranslatePromptSource
from app.application.prompting.finder_source import FinderPromptSource
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.local_source import LocalPromptSource
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import PromptResolver
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.agents import VariantSpec
from app.domain.classification import ClassificationResult
from app.domain.interaction import AgentRequest

# F-0 — variant 등록 + 3-Phase conductor 골격 + 전 신규 노드 명시 stub 의
# end-to-end(fake) 통과를 검증한다(빌드 순서 F-0). 도구 루프(F-4)·answer_spec
# 슬롯 산출(F-2)·multi-hop(F-5) 은 아직 stub.

_SPEC = VariantSpec(variant_id=AGENTIC_FINDER_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"
_OUTPUT_LANG = _REPO_PROMPTS / "system" / "output_language_v1.md"
_VOCAB = _REPO_PROMPTS.parent / "tools" / "terminology" / "vocab.yaml"


def _tool_registry_yaml(root: Path) -> Path:
    body = {
        "tools": {
            "retrieval.search": {"version": "v1", "adapter": "reranked", "timeout_ms": 6000, "retry": 1, "required": False},
            "retrieval.scope": {"version": "v1", "adapter": "corpus_map", "timeout_ms": 1000, "retry": 0, "required": False},
            "terminology.canonicalize": {"version": "v1", "adapter": "vocab", "timeout_ms": 1000, "retry": 0, "required": False},
            "submit_verdict": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
            "memory.session_load": {"version": "v1", "adapter": "postgres", "timeout_ms": 1000, "retry": 0, "required": False},
            "memory.session_update": {"version": "v1", "adapter": "postgres", "timeout_ms": 1000, "retry": 0, "required": False},
            "memory.approved_search": {"version": "v1", "adapter": "postgres_pgvector", "timeout_ms": 1000, "retry": 0, "required": False},
        }
    }
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _finder_tools(store) -> dict:
    """agentic_finder 변형이 invoke 하는 도구 set(F-3/F-4 + memory). LocalRetriever
    + IdentityReranker 로 fake 경로에서도 검색·정렬이 동작한다."""
    return {
        "retrieval.search": RetrievalSearchTool(
            retriever=LocalRetrieverTool(), reranker=IdentityReranker()),
        "retrieval.scope": RetrievalScopeTool(),
        "terminology.canonicalize": TerminologyCanonicalizeTool(
            vocab=TerminologyVocab.from_yaml(_VOCAB)),
        "submit_verdict": SubmitVerdictTool(),
        "memory.session_load": SessionLoadTool(store),
        "memory.session_update": SessionUpdateTool(store, ttl_days=90),
        "memory.approved_search": ApprovedSearchStubTool(),
    }


class _ScopeTierClassifier:
    """scope_tier 라우팅을 fixture 로 태우는 분류기. T1(기본 검색 경로)·T3(메타)·
    T4(deflect) 를 제어한다."""

    backend = "fake"
    policy_hash = "fake_finder"

    def __init__(self, *, scope_tier: str | None = None, confidence: float = 0.8,
                 entities: dict | None = None) -> None:
        self._tier = scope_tier
        self._conf = confidence
        self._entities = entities or {}

    async def classify(self, query_text, chat_history=()):
        return ClassificationResult(
            scenario_object="O4", scenario_depth="D2", entities=dict(self._entities),
            confidence=self._conf, object_confidence=self._conf,
            depth_confidence=self._conf, scope_tier=self._tier,
            classifier_backend=self.backend, classifier_policy_hash=self.policy_hash,
        )


def _make_runner(
    tmp: Path, *, classifier=None, llm=None, with_contract: bool = True,
    classification_threshold: float = 0.0,
) -> tuple[AgenticFinderRunner, FilesystemEventSink]:
    prompts = tmp / "prompts"
    build_prompts(prompts)
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    store = InMemorySessionMemoryStore()
    executor = ToolExecutor(registry=registry, tools=_finder_tools(store), event_sink=sink)
    llm_router = LLMRouter(
        pool={"fake-echo": llm or FakeEchoLLM(model_id="fake-echo")},
        default_id="fake-echo",
    )
    runner = AgenticFinderRunner(
        spec=_SPEC,
        llm_router=llm_router,
        tool_executor=executor,
        prompt_resolver=PromptResolver(LocalPromptSource(prompts)),
        prompt_renderer=PromptRenderer(),
        context_builder=ContextBuilder(capture_mode="snippets"),
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        classifier=classifier,
        classification_threshold=classification_threshold,
        citation_contract_path=str(_CONTRACT) if with_contract else None,
        output_language_contract_path=str(_OUTPUT_LANG) if with_contract else None,
        # 프롬프트는 registry 호스팅(코드 인라인 금지) — 실 repo prompts/ 에서 sha 검증과
        # 함께 로드한다(build_prompts 임시 fixture 엔 answer_spec/finder 블록 미존재).
        # source 미주입은 N0/N2/N3 부트 배선 오류라 항상 주입한다.
        query_translate_source=QueryTranslatePromptSource(_REPO_PROMPTS),
        answer_spec_source=AnswerSpecPromptSource(_REPO_PROMPTS),
        finder_source=FinderPromptSource(_REPO_PROMPTS),
    )
    return runner, sink


def _make_deps(tmp: Path, *, classifier=None, classification_prompt_source=None,
               utility_llm=None, finder_llm=None):
    """profiles.build_runtime 가 만드는 AgentDeps 번들을 fake 로 재현한다 — 변형이
    실제 선택되는 경로(VariantRegistry.build → _build_agentic_finder factory)를
    테스트가 타게 한다(직접 생성자 호출만으로는 factory·deps 배선이 미검증)."""
    from app.application.agents.registry import AgentDeps

    prompts = tmp / "prompts"
    build_prompts(prompts)
    registry = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    recorder = EventRecorder(sink, app_profile="local")
    store = InMemorySessionMemoryStore()
    executor = ToolExecutor(registry=registry, tools=_finder_tools(store), event_sink=sink)
    llm_router = LLMRouter(pool={"fake-echo": finder_llm or FakeEchoLLM(model_id="fake-echo")},
                           default_id="fake-echo")
    return AgentDeps(
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        llm_router=llm_router,
        utility_llm=utility_llm,
        tool_executor=executor,
        prompt_resolver=PromptResolver(LocalPromptSource(prompts)),
        prompt_renderer=PromptRenderer(),
        context_builder=ContextBuilder(capture_mode="snippets"),
        classifier=classifier,
        classification_prompt_source=classification_prompt_source,
        query_translate_prompt_source=QueryTranslatePromptSource(_REPO_PROMPTS),
        answer_spec_prompt_source=AnswerSpecPromptSource(_REPO_PROMPTS),
        finder_prompt_source=FinderPromptSource(_REPO_PROMPTS),
        tunables={
            "citation_contract_path": str(_CONTRACT),
            "output_language_contract_path": str(_OUTPUT_LANG),
        },
    )


@pytest.mark.asyncio
async def test_variant_is_registered() -> None:
    assert AGENTIC_FINDER_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_built_via_registry_factory_runs_finder_end_to_end() -> None:
    # 실제 선택 경로: VariantRegistry.build(...) 가 factory 를 호출하고, factory 가
    # AgentDeps 에서 필드를 골라 runner 를 조립한다(변형이 선택 가능). deps pool 에
    # FakeToolLLM 을 주입해 Finder 루프가 실 검색을 돌리는 경로까지 탄다.
    with tempfile.TemporaryDirectory() as tmp:
        deps = _make_deps(Path(tmp), classifier=_ScopeTierClassifier(),
                          finder_llm=_finder_script())
        runner = VariantRegistry.build(AGENTIC_FINDER_VARIANT_ID, _SPEC, deps)
        req = AgentRequest(interaction_id="fb1", query_text="i-SMR ECCS", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason is None
        assert resp.verification_status == "skipped"
        assert resp.answer_text
        assert len(resp.citations) >= 1   # Finder 검색 → chunk → 인용.


def test_factory_builds_classifier_from_prompt_source() -> None:
    # factory 의 classification_prompt_source.build_classifier(utility_llm) 분기 —
    # profiles 가 llm/hybrid backend 에서 타는 경로(직접 주입 테스트는 미검증).
    from app.application.prompting.classification_source import ClassificationPromptSource

    with tempfile.TemporaryDirectory() as tmp:
        source = ClassificationPromptSource(_REPO_PROMPTS)
        deps = _make_deps(
            Path(tmp), classifier=None,
            classification_prompt_source=source,
            utility_llm=FakeEchoLLM(model_id="fake-echo"),
        )
        runner = VariantRegistry.build(AGENTIC_FINDER_VARIANT_ID, _SPEC, deps)
        # deps.classifier 폴백이 아니라 source 로 빌드된 LLMClassifier 가 배선된다.
        assert runner._classifier is not None
        assert getattr(runner._classifier, "policy_hash", None) == source.policy_hash


def _finder_script():
    """scope→search→verdict 를 구동하는 FakeToolLLM 스크립트(1턴 1도구). 용어 정규화는
    N1.5 conductor(terminology.canonicalize)로 상향 — Finder 루프엔 normalize 없음."""
    from app.adapters.llm.fake import FakeToolLLM
    from app.ports.llm import LLMToolResult, ToolCall

    def r(*calls, stop="tool_calls"):
        return LLMToolResult(text="", tool_calls=tuple(calls), stop_reason=stop,
                             token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                             model_id="fake-tool")
    return FakeToolLLM(script=[
        r(ToolCall("c1", "retrieval.scope", {})),
        r(ToolCall("c3", "retrieval.search", {"query_text": "i-SMR ECCS", "top_k": 3})),
        r(ToolCall("c4", "submit_verdict", {"sufficient": True, "reason": "충족"})),
    ])


@pytest.mark.asyncio
async def test_finder_loop_searches_and_flows_chunks_to_generation() -> None:
    # Finder 가 실제 검색을 수행하면 chunk 가 누적되어 N6/N8 로 흘러 citations 가 채워진다.
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier(),
                                 llm=_finder_script())
        req = AgentRequest(interaction_id="fl1", query_text="i-SMR ECCS 요건", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason is None
        assert resp.verification_status == "skipped"   # 비동기 audit 만(런타임 게이트 없음).
        assert len(resp.citations) >= 1                # 검색 chunk → 인용.


@pytest.mark.asyncio
async def test_finder_search_tools_recorded_in_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier(),
                                 llm=_finder_script())
        req = AgentRequest(interaction_id="fl2", query_text="i-SMR ECCS", session_id="s1")
        await runner.run(req)
        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        rec = json.loads(next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0])
        names = {tc["name"] for tc in rec["tool_calls"]}
        # Finder 도구(scope/search/submit_verdict) + N1.5 conductor 의 terminology.
        # canonicalize 가 모두 ToolExecutor 경로로 기록된다("도구는 통제된다").
        assert {"retrieval.scope", "retrieval.search", "submit_verdict",
                "terminology.canonicalize"} <= names


@pytest.mark.asyncio
async def test_n15_canonicalize_pins_concepts_and_annotates_prompt() -> None:
    # N1.5 terminology.canonicalize(conductor-invoked, 보장) — 분류 entities("ECC")를
    # 용어집으로 정규화(ECC→ECCS)해 (1) 이벤트 재현 핀, (2) 생성 프롬프트 병기.
    with tempfile.TemporaryDirectory() as tmp:
        clf = _ScopeTierClassifier(entities={"system": ["ECC"], "reactor_type": ["i-SMR"]})
        runner, _ = _make_runner(Path(tmp), classifier=clf, llm=_finder_script())
        req = AgentRequest(interaction_id="tc1", query_text="i-SMR ECC 요건", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason is None

        root = Path(tmp) / "events" / "t"
        rec = json.loads(
            next((root / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        # (1) 재현 핀 — vocab_sha + 매핑된 concept_ids(ECC→ECCS, i-SMR).
        term = rec["query_understanding"]["terminology"]
        assert term["vocab_sha"]
        assert set(term["concept_ids"]) == {"ECCS", "i-SMR"}
        assert term["num_canonical"] == 2

        # (2) 생성 프롬프트에 정규형·정의 병기(검색 질의는 query_en 불변).
        prec = json.loads(
            next((root / "prompt_render_records").rglob("*.json")).read_text(encoding="utf-8")
        )
        prompt_text = prec["rendered_prompt"]
        assert "TERMINOLOGY" in prompt_text
        assert "ECCS" in prompt_text
        assert "Emergency Core Cooling System" in prompt_text  # 정의 병기.


@pytest.mark.asyncio
async def test_end_to_end_fake_skeleton_completes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier())
        req = AgentRequest(interaction_id="f1", query_text="i-SMR ECCS 설계특징", session_id="s1")
        resp = await runner.run(req)

        # F-0: 답변은 생성되되 chunks 미수집(finder stub) → citations 없음,
        # 생성 검증은 비동기 audit 만이라 응답 시점 SKIPPED(런타임 게이트 없음).
        assert resp.refusal_reason is None
        assert resp.verification_status == "skipped"
        assert resp.answer_text  # fake-echo 가 무언가 생성.
        assert resp.citations == ()
        assert resp.regulatory_grounding == "n_a"
        assert resp.scenario_object == "O4"


@pytest.mark.asyncio
async def test_event_recorded_with_variant_and_pins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier())
        req = AgentRequest(interaction_id="f2", query_text="질의", session_id="s1")
        await runner.run(req)

        events_root = Path(tmp) / "events" / "t" / "interaction_events"
        line = next(events_root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        rec = json.loads(line)
        assert rec["agent_variant"] == AGENTIC_FINDER_VARIANT_ID
        # 재현성 핀 — 분류 정책·렌더 프롬프트·컨텍스트 해시(원칙 #5).
        assert rec["classifier_policy_hash"]
        assert rec["rendered_prompt_hash"]
        assert rec["context_hash"]
        # 메모리 도구 3종이 invoke 되어 tool_calls 에 기록.
        names = {tc["name"] for tc in rec["tool_calls"]}
        assert {"memory.session_load", "memory.approved_search", "memory.session_update"} <= names


@pytest.mark.asyncio
async def test_n2_raises_when_answer_spec_source_not_wired() -> None:
    # 프롬프트는 registry 호스팅 — source 미주입은 부트 배선 오류(silent degrade 금지,
    # 분류/정보요구와 동일 fail-fast). T1 경로(검색 진입)에서만 N2 에 닿는다.
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier())
        runner._answer_spec_source = None
        req = AgentRequest(interaction_id="f7", query_text="질의", session_id="s1")
        with pytest.raises(RuntimeError, match="answer_spec_source not wired"):
            await runner.run(req)


@pytest.mark.asyncio
async def test_n0_raises_when_query_translate_source_not_wired() -> None:
    # N0 도 registry 호스팅 — source 미주입은 부트 배선 오류(N2/N3 와 동일 fail-fast).
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier())
        runner._query_translate_source = None
        req = AgentRequest(interaction_id="f8", query_text="질의", session_id="s1")
        with pytest.raises(RuntimeError, match="query_translate_source not wired"):
            await runner.run(req)


class _TransUtilLLM:
    """N0 translate JSON 을 돌려주는 utility fake(영어 질의 + 원 언어)."""

    model_id = "trans-util"

    async def generate(self, prompt, *, model_options=None, grammar=None):
        from app.ports.llm import LLMResult
        return LLMResult(
            text=json.dumps({
                "query_en": "ECCS performance requirements for i-SMR",
                "source_language": "Japanese",
            }),
            token_usage={"prompt_tokens": 1, "completion_tokens": 1},
            model_id=self.model_id,
        )

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_query_translate_pins_language_and_threads_english_prompt() -> None:
    # N0 가 영어 질의·원 언어를 산출 → (1) 이벤트에 번역 재현 핀, (2) 생성 프롬프트는
    # 영어 # QUERY + 출력-언어 지시문(source_language)으로 렌더된다.
    with tempfile.TemporaryDirectory() as tmp:
        deps = _make_deps(Path(tmp), classifier=_ScopeTierClassifier(),
                          utility_llm=_TransUtilLLM(), finder_llm=_finder_script())
        runner = VariantRegistry.build(AGENTIC_FINDER_VARIANT_ID, _SPEC, deps)
        req = AgentRequest(interaction_id="f9", query_text="i-SMRのECCS性能要件は？",
                           session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason is None

        root = Path(tmp) / "events" / "t"
        rec = json.loads(
            next((root / "interaction_events").rglob("*.jsonl"))
            .read_text(encoding="utf-8").splitlines()[0]
        )
        # (1) 재현 핀 — 번역은 워크플로우 상태(원칙 #5). 원질의 해시는 query_text_hash 가,
        # 내부 영어 질의·언어·정책은 query_understanding.query_translate 가 핀한다.
        qt = rec["query_understanding"]["query_translate"]
        assert qt["method"] == "llm"
        assert qt["source_language"] == "Japanese"
        assert qt["query_en_hash"] and qt["policy_hash"]
        assert rec["query_text_sample"] == "i-SMRのECCS性能要件は？"  # 원문 보존.

        # (2) 생성 프롬프트 — 영어 # QUERY + 출력-언어 지시문(Japanese).
        prec = json.loads(
            next((root / "prompt_render_records").rglob("*.json")).read_text(encoding="utf-8")
        )
        prompt_text = prec["rendered_prompt"]
        assert "ECCS performance requirements for i-SMR" in prompt_text  # 영어 질의 스레딩.
        assert "OUTPUT LANGUAGE" in prompt_text
        assert "Japanese" in prompt_text  # 최종 답변 언어 지시.


@pytest.mark.asyncio
async def test_t3_meta_short_circuits_without_search() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier(scope_tier="T3"))
        req = AgentRequest(interaction_id="f3", query_text="뭘 할 수 있어?", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason is None
        assert resp.scope_tier == "T3"
        assert "QA 어시스턴트" in resp.answer_text  # 고정 역량 서술.
        assert resp.citations == ()


@pytest.mark.asyncio
async def test_t4_deflect_refuses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier(scope_tier="T4"))
        req = AgentRequest(interaction_id="f4", query_text="잡담하자", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason == "out_of_scope"
        assert resp.verification_status == "skipped"


@pytest.mark.asyncio
async def test_low_confidence_requests_clarification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(
            Path(tmp), classifier=_ScopeTierClassifier(confidence=0.1),
            classification_threshold=0.5,
        )
        req = AgentRequest(interaction_id="f5", query_text="그거 어때", session_id="s1")
        resp = await runner.run(req)
        assert resp.refusal_reason == "clarification_required"


@pytest.mark.asyncio
async def test_run_stream_emits_step_events_then_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner, _ = _make_runner(Path(tmp), classifier=_ScopeTierClassifier())
        req = AgentRequest(interaction_id="f6", query_text="질의", session_id="s1")
        events = [ev async for ev in runner.run_stream(req)]
        kinds = [ev.kind for ev in events]
        assert kinds[-1] == "final"
        step_names = {ev.name for ev in events if ev.kind == "step"}
        # 3-Phase 신규 노드 step 이 방출된다.
        assert {"answer_spec", "finder_agent", "multi_hop_sequence"} <= step_names
        # token 이벤트(스트리밍 생성)도 존재.
        assert any(ev.kind == "token" for ev in events)
