from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.application.agents.spec_driven_v1 import (
    SPEC_DRIVEN_VARIANT_ID,
    SpecDrivenRunner,
    _assemble_final_chunks,
    _estimate_chunk_tokens,
    _render_spec_block,
    _select_with_slot_floor,
)
from app.application.context.pack import ContextBuilder
from app.application.intake.spec_driven_query import (
    _CANONICAL_FIELD,
    _DESIGN_FIELD,
    _STATUS_FIELD,
    _attach_targets,
    _dedup_queries,
    _ensure_references,
    _parse,
    _validate_canonical_id,
)
from app.application.events.recorder import EventRecorder
from app.application.prompting.spec_driven_source import (
    SpecDrivenAnswerSpecSource,
    SpecDrivenGeneralSource,
    SpecDrivenGenerationSource,
    SpecDrivenQuerySource,
    SpecDrivenTriageSource,
)
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry, ToolSpec
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest
from app.domain.spec_driven import AnswerSpec, FormulatedQuery, SpecSlot
from app.domain.tools import ToolResult
from app.ports.llm import LLMResult, LLMTokenDelta, LLMUnavailableError

# spec_driven_v1 — 4-Node 선형 conductor end-to-end(fake). VariantRegistry.build →
# factory → runner 의 실제 선택 경로를 탄다(deps 배선까지 검증).

_SPEC = VariantSpec(variant_id=SPEC_DRIVEN_VARIANT_ID)
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_CONTRACT = _REPO_PROMPTS / "system" / "citation_contract_v1.md"

_SPEC_JSON = json.dumps({
    "intent": "compliance",
    "explicit_references": ["10 CFR 50.46"],
    "governing_normative_class": "binding",
    "required_slots": [
        {"name": "governing_clause",
         "keywords": ["10 CFR 50.46", "ECCS acceptance criteria"], "required": True},
        {"name": "requirement_text",
         "keywords": ["peak cladding temperature"], "required": True},
    ],
    "answer_structure": "지배조문→정량 요건",
})
# N2 가 명시적 참조를 *빠뜨린* 쿼리(safety net 검증용 — ref 가 자동 합류돼야 한다).
_QUERIES_JSON = json.dumps({
    "queries": [
        {"slot_name": "governing_clause",
         "query_text": "ECCS acceptance criteria", "collection": "10CFR"},
        {"slot_name": "requirement_text",
         "query_text": "peak cladding temperature 2200 F"},
    ]
})
_ANSWER = "ECCS 요건은 PCT 2200°F 이하다 [cite-1]."
# N0 Triage 스크립트 — route=retrieval(기존 경로) / route=general(우회). N0 가 첫 generate.
_TRIAGE_RETRIEVAL = json.dumps(
    {"rationale": "특정 조문 지칭", "references_specifics": True, "route": "retrieval"}
)
_TRIAGE_GENERAL = json.dumps(
    {"rationale": "일반 개념 — 추론 가능", "references_specifics": False, "route": "general"}
)
# general 분기는 무근거이므로 [cite-N] 가 있으면 결정론 제거돼야 한다(검증용으로 마커 삽입).
_GENERAL_ANSWER = "심층방어는 다중 독립 방벽으로 안전을 확보하는 개념이다 [cite-1]."


class _ScriptLLM:
    """순차 generate() 스크립트(N1 spec → N2 queries → 비스트림 N4) + generate_stream
    (스트림 N4). _SpecLLM(test_answer_spec_intake) idiom 확장."""

    def __init__(self, *, gen_texts: list[str], stream_text: str = _ANSWER,
                 model_id: str = "fake") -> None:
        self._gen = list(gen_texts)
        self._i = 0
        self._stream = stream_text
        self.model_id = model_id

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        t = self._gen[min(self._i, len(self._gen) - 1)]
        self._i += 1
        return LLMResult(text=t, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None,
                              grammar=None) -> AsyncIterator[LLMTokenDelta]:
        yield LLMTokenDelta(content=self._stream, model_id=self.model_id,
                            token_usage={"prompt_tokens": 1, "completion_tokens": 5})

    async def generate_with_tools(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


class _UnavailableGenLLM(_ScriptLLM):
    """N0/N1/N2(generate)는 정상, N4 Generation(generate/stream)에서만 unavailable."""

    async def generate(self, prompt, *, model_options=None, grammar=None) -> LLMResult:
        if self._i >= 3:  # N0·N1·N2 후 N4(4번째 generate)에서만 실패.
            raise LLMUnavailableError("down")
        return await super().generate(prompt, model_options=model_options, grammar=grammar)

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):
        raise LLMUnavailableError("down")
        yield  # pragma: no cover


class _EmptyRetriever:
    """retriever.search 가 0건 반환 — gap-answer 경로 검증용."""

    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        return ToolResult(tool_name="retriever.search", tool_version="v1",
                          status="success", output={"chunks": []},
                          latency_ms=0, input_hash="x")


class _SpyRetriever(_EmptyRetriever):
    """호출 횟수를 센다 — general 분기가 retrieval.search 를 0회 호출하는지 검증용."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, tool_input, context) -> ToolResult:
        self.calls += 1
        return await super().invoke(tool_input, context)


class _RecordingRetriever(_EmptyRetriever):
    """invoke 에 전달된 RetrieverSearchInput 들을 기록한다 — agent 가 per-query
    collection filter 를 _NOISE_FILTER 와 merge 해 retriever 로 넘기는지 검증용
    (local retriever 는 filter 동작은 무시하므로 *입력*을 본다)."""

    def __init__(self) -> None:
        self.inputs: list[Any] = []

    async def invoke(self, tool_input, context) -> ToolResult:
        self.inputs.append(tool_input)
        return await super().invoke(tool_input, context)


def _tool_registry_yaml(root: Path) -> Path:
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked",
                             "timeout_ms": 6000, "retry": 0, "required": False},
    }}
    p = root / "tool_registry.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _deps(tmp: Path, *, llm, retriever=None) -> AgentDeps:
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
    return AgentDeps(
        recorder=recorder,
        event_sink=sink,
        app_profile="local",
        llm_router=llm_router,
        utility_llm=llm,  # 동일 인스턴스 — N1/N2/N4 generate 순차 cursor.
        tool_executor=executor,
        context_builder=ContextBuilder(capture_mode="snippets"),
        spec_driven_answer_spec_source=SpecDrivenAnswerSpecSource(_REPO_PROMPTS),
        spec_driven_query_source=SpecDrivenQuerySource(_REPO_PROMPTS),
        spec_driven_generation_source=SpecDrivenGenerationSource(_REPO_PROMPTS),
        spec_driven_triage_source=SpecDrivenTriageSource(_REPO_PROMPTS),
        spec_driven_general_source=SpecDrivenGeneralSource(_REPO_PROMPTS),
        tunables={
            "citation_contract_path": str(_CONTRACT),
            "retriever_top_k": 3,
            "spec_driven_max_queries": 6,
        },
    )


def _script(gen_texts: list[str] | None = None) -> _ScriptLLM:
    # N0 Triage(retrieval) 가 첫 generate — 기존 retrieval 경로를 그대로 탄다.
    return _ScriptLLM(
        gen_texts=gen_texts or [_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _ANSWER]
    )


def _build(tmp: Path, llm, retriever=None) -> SpecDrivenRunner:
    return VariantRegistry.build(SPEC_DRIVEN_VARIANT_ID, _SPEC,
                                 _deps(tmp, llm=llm, retriever=retriever))


def _event(tmp: Path) -> dict:
    root = Path(tmp) / "events" / "t" / "interaction_events"
    line = next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def _req() -> AgentRequest:
    return AgentRequest(interaction_id="ix1",
                        query_text="10 CFR 50.46 ECCS 요건은?", model="fake")


def test_variant_is_registered() -> None:
    assert SPEC_DRIVEN_VARIANT_ID in VariantRegistry.known()


@pytest.mark.asyncio
async def test_end_to_end_grounded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        assert resp.answer_text == _ANSWER
        assert len(resp.citations) > 0  # LocalRetriever fixtures → 근거 있음.
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["evidence_gap"] is False
        assert pin["spec"]["intent"] == "compliance"
        assert pin["spec"]["explicit_references"] == ["10 CFR 50.46"]
        assert pin["spec"]["method"] == "llm"
        assert pin["formulation"]["num_queries"] == 2
        # 1차 검색 전량 보존 — follow_up 미배선이라 최종 == 1차(no cap drop).
        ret = pin["retrieval"]
        assert ret["first_pass_kept"] == ret["num_chunks"]
        # 토큰 예산 핀(budget=0 무제한 → drop 없음)이 항상 기록된다(원칙 5).
        cb = pin["context_budget"]
        assert cb["budget"] == 0
        assert cb["dropped_chunk_ids"] == []
        assert cb["first_pass_dropped"] is False


@pytest.mark.asyncio
async def test_explicit_reference_lands_in_query_verbatim() -> None:
    # N2 가 첫 쿼리에서 "10 CFR 50.46" 을 빠뜨렸어도 safety net 이 verbatim 합류시킨다.
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        await runner.run(_req())
        queries = _event(tmp)["query_understanding"]["spec_driven"]["formulation"]["queries"]
        joined = " ".join(q["query_text"] for q in queries)
        assert "10 CFR 50.46" in joined
        # collection boost 가 결정론적으로 유도된다(10 CFR → 10CFR).
        assert any(q["target"].get("collection") == ["10CFR"] for q in queries)


# === collection boost vs filter 모드 선택(#2 확장) ================================

# N2 가 filter 모드 + nuscale_FSAR 를 고른 쿼리(명시적 참조 없음 — safety net 비개입).
_FILTER_QUERIES_JSON = json.dumps({
    "queries": [
        {"slot_name": "design_feature",
         "query_text": "NuScale decay heat removal passive natural circulation",
         "collection": "nuscale_FSAR", "collection_mode": "filter"},
    ]
})
_SPEC_NO_REF_JSON = json.dumps({
    "intent": "design_description",
    "explicit_references": [],
    "governing_normative_class": "applicant_claim",
    "required_slots": [
        {"name": "design_feature", "keywords": ["NuScale", "decay heat removal"],
         "required": True},
    ],
    "answer_structure": "설계 기술",
})


@pytest.mark.asyncio
async def test_filter_mode_reaches_retriever_merged_with_noise_filter() -> None:
    # 모델이 collection_mode=filter 를 고르면 hard-scope 가 _NOISE_FILTER 와 merge 돼
    # retriever 입력으로 흐르고, 재현 핀에 filters/mode 가 기록된다.
    with tempfile.TemporaryDirectory() as tmp:
        rec = _RecordingRetriever()
        llm = _ScriptLLM(gen_texts=[
            _TRIAGE_RETRIEVAL, _SPEC_NO_REF_JSON, _FILTER_QUERIES_JSON, _ANSWER])
        runner = _build(Path(tmp), llm, retriever=rec)
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        # retriever 가 받은 입력: noise floor + collection hard-filter.
        assert rec.inputs, "retriever should have been invoked"
        ri = rec.inputs[0]
        assert ri.filters == {"noise": False, "collection": ["nuscale_FSAR"]}
        assert ri.target == {}  # filter 모드라 boost 채널은 비어 있다.
        # 재현 핀(원칙 5).
        q = _event(tmp)["query_understanding"]["spec_driven"]["formulation"]["queries"][0]
        assert q["filters"] == {"collection": ["nuscale_FSAR"]}
        assert q["target"] == {}
        assert q["mode"] == "filter"


# === N3.5 멀티홉(follow-up) 2차 검색 — 관련성 게이트·noise 필터 검증 ============

class _FakeFollowUpTool:
    """retrieval.follow_up — 고정 follow-up 쿼리 1건(대상 source_id 지정) 반환."""

    name = "retrieval.follow_up"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        return ToolResult(
            tool_name="retrieval.follow_up", tool_version="v1", status="success",
            output={"follow_up_queries": [
                {"query_text": "peak cladding temperature limit",
                 "target_source_ids": ["ML_TARGET"], "intent": "정량 한계"},
            ]},
            latency_ms=0, input_hash="x", output_hash="y", trace_id="",
        )


class _SecondPassRetriever:
    """1차는 1건, source_id 필터가 걸린 2차는 keep_k 보다 많은(5건) chunk 를 score
    desc 로 반환한다 — 관련성 게이트(상위 keep_k 만 채택)와 2차 noise 필터 검증용."""

    name = "retriever.search"
    version = "v1"

    def __init__(self) -> None:
        self.inputs: list[Any] = []

    async def invoke(self, tool_input, context) -> ToolResult:
        self.inputs.append(tool_input)
        is_second = bool((tool_input.filters or {}).get("source_id"))
        if not is_second:
            chunks = [{"chunk_id": "first1", "document_id": "D1", "score": 0.9,
                       "snippet": "ECCS first-pass"}]
        else:
            # 5건 — keep_k(기본 3) 초과. score desc.
            chunks = [
                {"chunk_id": f"sec{i}", "document_id": "D2",
                 "score": 0.8 - i * 0.1, "snippet": f"sec body {i}"}
                for i in range(5)
            ]
        return ToolResult(tool_name="retriever.search", tool_version="v1",
                          status="success", output={"chunks": chunks},
                          latency_ms=0, input_hash="x", trace_id="")


def _deps_with_follow_up(tmp: Path, *, llm, retriever) -> AgentDeps:
    deps = _deps(tmp, llm=llm, retriever=retriever)
    # follow_up 도구를 executor 에 등록(registry 에도 정책 추가).
    reg = ToolRegistry.from_yaml(_tool_registry_yaml(tmp))
    reg._specs["retrieval.follow_up"] = ToolSpec(  # noqa: SLF001
        name="retrieval.follow_up", version="v1", adapter="fake",
        timeout_ms=1000, retry=0, required=False,
    )
    tools = {
        "retrieval.search": RetrievalSearchTool(
            retriever=retriever, reranker=IdentityReranker()),
        "retrieval.follow_up": _FakeFollowUpTool(),
    }
    deps.tool_executor = ToolExecutor(registry=reg, tools=tools,
                                      event_sink=deps.event_sink)
    return deps


@pytest.mark.asyncio
async def test_follow_up_second_pass_relevance_gate_and_noise_filter() -> None:
    # 2차 검색은 (1) source_id hard-scope + noise:false 필터를 싣고, (2) 쿼리당
    # 상위 keep_k(기본 3)개만 컨텍스트에 채택한다(저관련 tail 배제 — "필요·중요 내용만").
    with tempfile.TemporaryDirectory() as tmp:
        rec = _SecondPassRetriever()
        llm = _ScriptLLM(gen_texts=[
            _TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _ANSWER])
        deps = _deps_with_follow_up(Path(tmp), llm=llm, retriever=rec)
        runner = VariantRegistry.build(SPEC_DRIVEN_VARIANT_ID, _SPEC, deps)
        await runner.run(_req())

        # 2차 검색 입력(source_id 필터가 걸린 것)을 찾는다.
        second = [i for i in rec.inputs if (i.filters or {}).get("source_id")]
        assert second, "second-pass search should have fired"
        si = second[0]
        assert si.filters.get("source_id") == ["ML_TARGET"]
        assert si.filters.get("noise") is False          # noise 필터 동승
        assert si.min_token_count == 0                    # min_token_count 동승(기본 0)

        # 재현 핀 — 5건 중 상위 keep_k(=3)만 added.
        pin = _event(tmp)["query_understanding"]["spec_driven"]["follow_up"]
        assert pin["num_queries"] == 1
        assert pin["added_chunks"] == 3


def test_parse_routes_filter_to_filters_boost_to_target() -> None:
    # collection_mode=filter → filters; 누락/boost → target. 확장 enum(nuscale_*) 수용.
    qs = _parse(json.dumps({"queries": [
        {"slot_name": "a", "query_text": "x",
         "collection": "nuscale_SER", "collection_mode": "filter"},
        {"slot_name": "b", "query_text": "y", "collection": "nuscale_RAI"},  # mode 무 → boost
        {"slot_name": "c", "query_text": "z",
         "collection": "BOGUS", "collection_mode": "filter"},  # 미지 collection → drop
    ]}))
    by_slot = {q.slot_name: q for q in qs}
    assert by_slot["a"].filters == {"collection": ["nuscale_SER"]}
    assert by_slot["a"].target == {}
    assert by_slot["b"].target == {"collection": ["nuscale_RAI"]}
    assert by_slot["b"].filters == {}
    assert by_slot["c"].filters == {} and by_slot["c"].target == {}


def test_attach_targets_never_escalates_model_filter_to_boost() -> None:
    # 모델이 filter 를 고른 쿼리는 query_text 에 ref 가 있어도 boost 를 유도하지 않고
    # filter 를 보존한다(안전망은 boost 전용).
    q = FormulatedQuery(slot_name="s", query_text="10 CFR 50.46 ECCS",
                        filters={"collection": ["nuscale_FSAR"]})
    (out,) = _attach_targets((q,))
    assert out.target == {}  # boost 유도 안 됨(filter 가 이미 있음)
    assert out.filters == {"collection": ["nuscale_FSAR"]}
    # collection 없는 쿼리는 boost 만 유도(filter 아님).
    q2 = FormulatedQuery(slot_name="s2", query_text="RG 1.97 monitoring")
    (out2,) = _attach_targets((q2,))
    assert out2.target == {"collection": ["RG"]}
    assert out2.filters == {}


def test_ensure_references_preserves_filters() -> None:
    # safety net (1) 이 ref 를 합류시키며 rebuild 할 때 모델 filter 를 유실하지 않는다.
    q = FormulatedQuery(slot_name="s", query_text="ECCS acceptance criteria",
                        filters={"collection": ["nuscale_FSAR"]})
    (out,) = _ensure_references((q,), ("10 CFR 50.46",))
    assert "10 CFR 50.46" in out.query_text  # ref 합류
    assert out.filters == {"collection": ["nuscale_FSAR"]}  # filter 보존


def test_dedup_queries_collapses_identical_query_text_across_slots() -> None:
    # safety net (3): 소형 모델이 같은 query_text 를 N개 슬롯에 복제해도 동일 검색은
    # 1회로 접힌다(scope 동일 시). 대소문자/공백만 다른 것도 같은 것으로 본다.
    qs = (
        FormulatedQuery(slot_name="structure", query_text="NuScale FSAR section 5.4.1",
                        filters={"collection": ["nuscale_FSAR"]}),
        FormulatedQuery(slot_name="content", query_text="nuscale fsar  section 5.4.1",
                        filters={"collection": ["nuscale_FSAR"]}),
        FormulatedQuery(slot_name="method", query_text="NuScale FSAR section 5.4.1",
                        filters={"collection": ["nuscale_FSAR"]}),
    )
    out = _dedup_queries(qs)
    assert len(out) == 1
    assert out[0].slot_name == "structure"  # 첫 쿼리만 남는다


def test_dedup_queries_keeps_distinct_text_and_distinct_scope() -> None:
    # 다른 query_text 는 보존; query_text 가 같아도 scope(boost vs filter, collection)가
    # 다르면 별개 검색이라 보존한다.
    qs = (
        FormulatedQuery(slot_name="a", query_text="GDC 35 emergency core cooling",
                        target={"collection": ["10CFR"]}),
        FormulatedQuery(slot_name="b", query_text="NuScale ECCS design FSAR",
                        filters={"collection": ["nuscale_FSAR"]}),
        # 같은 텍스트지만 filter vs boost → 별개.
        FormulatedQuery(slot_name="c", query_text="GDC 35 emergency core cooling",
                        filters={"collection": ["10CFR"]}),
    )
    out = _dedup_queries(qs)
    assert len(out) == 3


# === 검색 스코프 메타데이터 (status / design / canonical_id) =====================
# 설계: docs/plans/spec_driven_search_scope_metadata.design.v1.md.


def test_parse_status_only_on_regulatory_collections() -> None:
    # status 는 RG/SRP/DSRS 에만 합성된다(§4.3). 비규제 collection 의 status 는 무시되고
    # scope_audit.status_dropped 로 기록된다(silent drop 금지 — 원칙 6).
    qs = _parse(json.dumps({"queries": [
        {"slot_name": "rg", "query_text": "RG 1.206", "collection": "RG",
         "status": "current", "status_mode": "filter"},
        {"slot_name": "cfr", "query_text": "10 CFR 50.46", "collection": "10CFR",
         "status": "current"},  # 10CFR 엔 status 없음 → 무시 + dropped
        {"slot_name": "nu", "query_text": "FSAR ECCS", "collection": "nuscale_FSAR",
         "status": "current"},  # NuScale 엔 status 없음 → 무시 + dropped
    ]}))
    by = {q.slot_name: q for q in qs}
    assert by["rg"].filters[_STATUS_FIELD] == ["current"]
    assert _STATUS_FIELD not in by["cfr"].filters and _STATUS_FIELD not in by["cfr"].target
    assert by["cfr"].scope_audit.get("status_dropped") is True
    assert by["nu"].scope_audit.get("status_dropped") is True


def test_parse_design_only_on_nuscale_collections() -> None:
    # design 은 nuscale_* 에만 합성된다(§5.3). 규제 collection 의 design 은 무시 + dropped.
    qs = _parse(json.dumps({"queries": [
        {"slot_name": "nu", "query_text": "FSAR ECCS", "collection": "nuscale_FSAR",
         "design": "US_600", "design_mode": "filter"},
        {"slot_name": "rg", "query_text": "RG 1.206", "collection": "RG",
         "design": "US_460"},  # 규제 collection → 무시 + dropped
        {"slot_name": "bad", "query_text": "FSAR", "collection": "nuscale_FSAR",
         "design": "US_999"},  # enum 외 → 미설정(값 자체가 무효라 audit 도 안 함)
    ]}))
    by = {q.slot_name: q for q in qs}
    assert by["nu"].filters[_DESIGN_FIELD] == ["US_600"]
    assert _DESIGN_FIELD not in by["rg"].filters and _DESIGN_FIELD not in by["rg"].target
    assert by["rg"].scope_audit.get("design_dropped") is True
    assert _DESIGN_FIELD not in by["bad"].target and _DESIGN_FIELD not in by["bad"].filters
    assert "design_dropped" not in by["bad"].scope_audit  # 무효값은 drop 이 아님


def test_parse_canonical_id_gate_pass_and_reject() -> None:
    # canonical_id 게이트(§5b.2): 정규식 + collection prefix 정합 통과 시 승격, 실패 시
    # 버림 + canonical_id_rejected.
    qs = _parse(json.dumps({"queries": [
        {"slot_name": "ok", "query_text": "RG 1.206", "collection": "RG",
         "canonical_id": "RG-1.206", "canonical_id_mode": "filter"},
        {"slot_name": "mismatch", "query_text": "RG 1.206", "collection": "SRP",
         "canonical_id": "RG-1.206"},  # prefix RG ↔ collection SRP 불일치 → 기각
        {"slot_name": "malformed", "query_text": "x", "collection": "RG",
         "canonical_id": "Regulatory Guide 1.206"},  # 정규식 불일치 → 기각
    ]}))
    by = {q.slot_name: q for q in qs}
    assert by["ok"].filters[_CANONICAL_FIELD] == ["RG-1.206"]
    assert _CANONICAL_FIELD not in by["mismatch"].filters
    assert by["mismatch"].scope_audit.get("canonical_id_rejected") is True
    assert by["malformed"].scope_audit.get("canonical_id_rejected") is True


def test_validate_canonical_id_forms() -> None:
    assert _validate_canonical_id("RG-1.206", "RG") == "RG-1.206"
    assert _validate_canonical_id("SRP-15.6.5", "SRP") == "SRP-15.6.5"
    assert _validate_canonical_id("DSRS-10.3", "DSRS") == "DSRS-10.3"
    assert _validate_canonical_id("10CFR-Part1-50", "10CFR") == "10CFR-Part1-50"
    # collection 미지정이면 prefix 자체가 collection 을 함의 → 통과.
    assert _validate_canonical_id("RG-1.206", None) == "RG-1.206"
    # prefix 불일치 / 비정형 → None.
    assert _validate_canonical_id("RG-1.206", "10CFR") is None
    assert _validate_canonical_id("RG 1.206", "RG") is None
    assert _validate_canonical_id("Letter-PreApp", "nuscale_Letter") is None


def test_parse_boost_mode_routes_scope_to_target() -> None:
    # boost 모드(기본)는 채널을 target 에 싣는다(recall-safe 가산).
    (q,) = _parse(json.dumps({"queries": [
        {"slot_name": "s", "query_text": "RG 1.97", "collection": "RG",
         "collection_mode": "boost", "status": "current", "status_mode": "boost"},
    ]}))
    assert q.target["collection"] == ["RG"]
    assert q.target[_STATUS_FIELD] == ["current"]
    assert q.filters == {}


def test_dedup_distinguishes_status_scope() -> None:
    # 같은 query_text·collection 이라도 status 가 다르면 별개 검색이라 접지 않는다.
    qs = (
        FormulatedQuery(slot_name="cur", query_text="RG 1.206",
                        filters={"collection": ["RG"], _STATUS_FIELD: ["current"]}),
        FormulatedQuery(slot_name="hist", query_text="RG 1.206",
                        filters={"collection": ["RG"], _STATUS_FIELD: ["history"]}),
    )
    assert len(_dedup_queries(qs)) == 2


def test_attach_targets_preserves_canonical_boost_when_deriving_collection() -> None:
    # collection 없이 boost 모드 canonical_id 만 있는 쿼리에 collection 을 유도할 때,
    # 기존 canonical boost 채널을 파괴하지 않는다(merge — §_attach_targets).
    q = FormulatedQuery(slot_name="s", query_text="RG 1.206 scope",
                        target={_CANONICAL_FIELD: ["RG-1.206"]})
    (out,) = _attach_targets((q,))
    assert out.target[_CANONICAL_FIELD] == ["RG-1.206"]  # 보존
    assert out.target["collection"] == ["RG"]  # query_text 에서 유도 추가


@pytest.mark.asyncio
async def test_gap_answer_on_zero_chunks_not_refusal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script(), retriever=_EmptyRetriever())
        resp = await runner.run(_req())
        assert resp.refusal_reason is None  # gap-answer 는 거부 아님(사용자 #3).
        assert resp.citations == ()  # 근거 0건 → 인용 없음.
        # 무근거 [cite-N] 마커는 결정론 backstop 으로 제거된다(advisor #2).
        assert "[cite-" not in resp.answer_text
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["evidence_gap"] is True
        assert pin["retrieval"]["num_chunks"] == 0


@pytest.mark.asyncio
async def test_n1_unparseable_falls_back() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp),
                        _script([_TRIAGE_RETRIEVAL, "not json", _QUERIES_JSON, _ANSWER]))
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["spec"]["method"] == "fallback"
        # fallback spec 도 쿼리·답을 낸다(silent degrade 아님, method 기록).


@pytest.mark.asyncio
async def test_llm_unavailable_during_generation_refuses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = _UnavailableGenLLM(
            gen_texts=[_TRIAGE_RETRIEVAL, _SPEC_JSON, _QUERIES_JSON, _ANSWER])
        runner = _build(Path(tmp), llm)
        resp = await runner.run(_req())
        assert resp.refusal_reason == "llm_unavailable"


@pytest.mark.asyncio
async def test_run_stream_emits_steps_tokens_then_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        kinds = []
        final = None
        async for ev in runner.run_stream(_req()):
            kinds.append(ev.kind)
            if ev.kind == "final":
                final = ev.payload["response"]
        assert "step" in kinds and "token" in kinds and "final" in kinds
        assert final is not None and final.refusal_reason is None


def test_gap_block_present_only_on_evidence_gap_and_language_recency() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        spec = AnswerSpec(
            intent="compliance", explicit_references=("10 CFR 50.46",),
            required_slots=(SpecSlot(name="governing_clause", keywords=("x",)),),
            answer_structure="a→b", governing_normative_class="binding",
        )
        pack = runner._context_builder.build(
            interaction_id="ix", query_text="q", chat_history=(),
            conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
            entities={}, chunks=[], memory_refs=(), tool_result_refs=(),
        )
        grounded = runner._render_generation_prompt("q", pack, spec, evidence_gap=False)
        gap = runner._render_generation_prompt("q", pack, spec, evidence_gap=True)
        assert "# ANSWER SPEC" in grounded
        assert "# EVIDENCE GAP" not in grounded
        assert "# EVIDENCE GAP (NO RESULTS)" in gap
        # 언어 규칙 trailer 는 최고 recency(맨 끝).
        assert grounded.rstrip().endswith("verbatim.")


def test_render_spec_block_shape() -> None:
    spec = AnswerSpec(intent="definition", explicit_references=("RG 1.157",),
                      required_slots=(SpecSlot(name="definition", keywords=("a",)),))
    block = _render_spec_block(spec)
    assert "intent: definition" in block
    assert "explicit_references: RG 1.157" in block


# === N0 Triage / N4-G General Generation (RAG 비대상 도메인 질의 우회) =============

def _req_general() -> AgentRequest:
    return AgentRequest(interaction_id="ixg",
                        query_text="심층방어(defense in depth)의 기본 개념은?",
                        model="fake")


@pytest.mark.asyncio
async def test_general_route_bypasses_retrieval() -> None:
    # N0 가 route=general → N1/N2/N3 우회, retrieval.search 0회. 1급 outcome.
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpyRetriever()
        # general 분기 generate cursor: N0(triage) → N4-G(answer). 2콜.
        llm = _ScriptLLM(gen_texts=[_TRIAGE_GENERAL, _GENERAL_ANSWER])
        runner = _build(Path(tmp), llm, retriever=spy)
        resp = await runner.run(_req_general())
        assert spy.calls == 0  # 검색 도구 한 번도 안 부른다.
        assert resp.refusal_reason is None
        assert resp.regulatory_grounding == "parametric"  # grounded 아님 — 감사 구별.
        assert resp.citations == ()
        # 무근거 [cite-N] 마커는 결정론 backstop 으로 제거된다.
        assert "[cite-" not in resp.answer_text
        assert resp.answer_text.startswith("심층방어는 다중 독립 방벽")
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["route"] == "general"
        assert pin["triage"]["route"] == "general"
        assert pin["triage"]["method"] == "llm"
        # general 분기는 spec/formulation/retrieval 백을 남기지 않는다(노드 미실행).
        assert "spec" not in pin and "formulation" not in pin


@pytest.mark.asyncio
async def test_triage_unparseable_degrades_to_retrieval() -> None:
    # N0 응답 파싱불가 → 라우팅 근거 없음 → 안전 degrade(retrieval). 라우팅 규칙 아님.
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp),
                        _script(["not json", _SPEC_JSON, _QUERIES_JSON, _ANSWER]))
        resp = await runner.run(_req())
        assert resp.refusal_reason is None
        pin = _event(tmp)["query_understanding"]["spec_driven"]
        assert pin["route"] == "retrieval"  # degrade 로 검색 경로.
        assert pin["triage"]["method"] == "fallback"
        assert pin["spec"]["method"] == "llm"  # 이후 N1 정상.


@pytest.mark.asyncio
async def test_general_route_streams_tokens_then_final() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        llm = _ScriptLLM(gen_texts=[_TRIAGE_GENERAL], stream_text=_GENERAL_ANSWER)
        runner = _build(Path(tmp), llm, retriever=_SpyRetriever())
        kinds = []
        final = None
        async for ev in runner.run_stream(_req_general()):
            kinds.append(ev.kind)
            if ev.kind == "final":
                final = ev.payload["response"]
        assert "step" in kinds and "token" in kinds and "final" in kinds
        assert final is not None and final.regulatory_grounding == "parametric"


def test_render_general_prompt_no_context_block() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        text = runner._render_general_prompt("심층방어란?")
        assert "# CONTEXT" not in text  # 근거 블록 없음.
        assert "# ANSWER SPEC" not in text
        assert "# QUERY\n심층방어란?" in text
        # 출력-언어 trailer 가 최고 recency(맨 끝).
        assert "# RESPONSE LANGUAGE" in text
        assert text.rstrip().endswith("Korean answer).")


# ── per-slot floor 선택(설계 §3.2) — 순수 함수 단위 검증 ──────────────────────
from app.domain.retrieval import RetrievedChunk  # noqa: E402


def _ch(cid: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=cid, document_id="d", score=score)


def test_slot_floor_retains_low_score_required_slot() -> None:
    # required 슬롯 z 의 유일 chunk(C)가 score 최하위라 전역 top-2 면 잘리지만,
    # floor 가 먼저 확보 → C 생존, 나머지 예산은 최고 score A 로 채움(B 탈락).
    merged = [_ch("A", 0.9), _ch("B", 0.8), _ch("C", 0.3)]
    slots = {"A": {"x"}, "B": {"y"}, "C": {"z"}}
    chunks, cov = _select_with_slot_floor(merged, slots, ("z",), budget=2)
    ids = [c.chunk_id for c in chunks]
    assert "C" in ids and "A" in ids and "B" not in ids
    assert ids == ["A", "C"]  # 렌더 순서는 score desc 유지
    assert cov["floored_slots"] == ["z"]
    assert cov["uncovered_required"] == []


def test_slot_floor_marks_uncovered_when_slot_has_no_chunk() -> None:
    chunks, cov = _select_with_slot_floor([_ch("A", 0.9)], {"A": {"x"}},
                                          ("z",), budget=8)
    assert cov["uncovered_required"] == ["z"]
    assert cov["covered_required"] == []


def test_slot_floor_budget_smaller_than_required_count() -> None:
    # budget 1 < required 2 → 앞선 required(p)만 floor, q 는 uncovered(no silent loss).
    merged = [_ch("A", 0.9), _ch("B", 0.8)]
    slots = {"A": {"p"}, "B": {"q"}}
    chunks, cov = _select_with_slot_floor(merged, slots, ("p", "q"), budget=1)
    assert [c.chunk_id for c in chunks] == ["A"]
    assert cov["floored_slots"] == ["p"]
    assert cov["uncovered_required"] == ["q"]


def test_slot_floor_no_required_is_pure_topk() -> None:
    # required 없음 → 순수 score top-K(기존 동작과 동일).
    merged = [_ch("A", 0.9), _ch("B", 0.8), _ch("C", 0.3)]
    chunks, cov = _select_with_slot_floor(merged, {}, (), budget=2)
    assert [c.chunk_id for c in chunks] == ["A", "B"]
    assert cov["floored_slots"] == []


@pytest.mark.asyncio
async def test_context_budget_default_and_fetch_fills_to_budget() -> None:
    # N3 floor 정렬 budget=20(기본, 컨텍스트 확장) + per-query fetch=budget 으로 단일
    # 쿼리로도 budget 까지 채울 수 있게 구성. 최종 cap 은 token budget(=0=무제한)이라
    # num_chunks 는 1차 floor budget(20) 안에서만 검증한다(no silent final cap).
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        assert runner._max_context_chunks == 20
        await runner.run(_req())
        pin = _event(tmp)["query_understanding"]["spec_driven"]["retrieval"]
        assert pin["budget"] == 20           # N3 floor 정렬 budget = 20(확장)
        assert pin["fetch_k"] == 20          # per-query fetch = budget(top_k 3 < 20)
        # num_chunks 의 최종 cap 은 context_token_budget 이 지배한다(=0=무제한). N3 floor
        # budget(10)은 더 이상 최종 상한이 아니므로 1차 전량 보존을 확인한다.
        assert pin["num_chunks"] == pin["first_pass_kept"]


# ── 최종 조립(1차 전량 + 2차 score 순, 토큰 예산) — 순수 함수 단위 검증 ──────────
def _chs(cid: str, score: float, snippet: str = "") -> RetrievedChunk:
    return RetrievedChunk(chunk_id=cid, document_id="d", score=score, snippet=snippet)


def test_assemble_no_budget_keeps_all_first_and_second() -> None:
    # budget=0 → 캡 없음. 1차(A,C) 전량 + 2차(B,D) 전량, 최종은 score desc.
    merged = [_chs("A", 0.9), _chs("B", 0.8), _chs("C", 0.5), _chs("D", 0.4)]
    first_pass = {"A", "C"}
    chunks, log, total, fp_dropped = _assemble_final_chunks(first_pass, merged, 0)
    ids = [c.chunk_id for c in chunks]
    # Phase A(1차: A,C) → Phase B(2차: B,D). 1차 우선 배치.
    assert ids == ["A", "C", "B", "D"]
    assert log == [] and fp_dropped is False


def test_assemble_budget_drops_second_pass_tail_first() -> None:
    # 각 chunk ~ snippet 30자 → ~10+12=22 토큰. budget 으로 2차 일부만 컷.
    snip = "x" * 30
    merged = [_chs("A", 0.9, snip), _chs("B", 0.8, snip),
              _chs("C", 0.5, snip), _chs("D", 0.4, snip)]
    first_pass = {"A", "C"}  # 1차
    per = _estimate_chunk_tokens(merged[0])
    # 3개분 예산 → 2차 tail(D=최저 score)부터 drop, 1차는 보존.
    budget = per * 3
    chunks, log, total, fp_dropped = _assemble_final_chunks(first_pass, merged, budget)
    ids = [c.chunk_id for c in chunks]
    assert "A" in ids and "C" in ids        # 1차 전량 보존
    assert "D" in log                        # 2차 최저 score drop
    assert fp_dropped is False
    assert total <= budget


def test_assemble_first_pass_dropped_only_as_last_resort() -> None:
    # 예산이 1차만으로도 초과 → 2차 전멸 후 1차 tail drop(최후 안전판) + 플래그.
    snip = "y" * 30
    merged = [_chs("A", 0.9, snip), _chs("B", 0.8, snip), _chs("C", 0.5, snip)]
    first_pass = {"A", "B", "C"}  # 전부 1차, 2차 없음
    per = _estimate_chunk_tokens(merged[0])
    budget = per * 2  # 2개분만 허용
    chunks, log, total, fp_dropped = _assemble_final_chunks(first_pass, merged, budget)
    assert fp_dropped is True
    assert "C" in log                        # 최저 score 1차부터 drop
    assert len(chunks) == 2 and total <= budget


# ── thinking 은 모델 출력 중심 ───────────────────────────────────────────────
# spec_driven_v1 은 step renderer 를 우회하므로(thinking_renderer._LLM_THINKING_VARIANTS)
# Thought 블록은 모델 산출(N0 triage.rationale · N1/N2/N4 native CoT)로 구성된다. runner
# 는 enum/카운트를 재서술한 정해진 텍스트를 싣지 않는다.

# 결정론 재서술(정해진 텍스트) — 절대 thinking 에 나와선 안 되는 문자열.
_CANNED_THINKING = ("라우팅:", "답변 사양:", "검색 쿼리", "근거 검색", "확보")


async def _collect_reasoning_stream(runner, req) -> tuple[object, str]:
    """run_stream 을 끝까지 돌려 reasoning 이벤트 content 를 이어붙인다."""
    final = None
    texts: list[str] = []
    async for ev in runner.run_stream(req):
        if ev.kind == "reasoning":
            texts.append(ev.payload.get("content", ""))
        if ev.kind == "final":
            final = ev.payload["response"]
    return final, "".join(texts)


@pytest.mark.asyncio
async def test_thinking_surfaces_model_triage_rationale_not_canned() -> None:
    # N0 thinking 은 모델이 쓴 판정 사유(triage.rationale)를 그대로 보인다 — route enum/
    # 플래그를 재서술한 정해진 텍스트가 아니다.
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script())
        final, think = await _collect_reasoning_stream(runner, _req())
        assert final is not None and final.refusal_reason is None
        assert "특정 조문 지칭" in think          # 모델 산출(rationale)
        for canned in _CANNED_THINKING:
            assert canned not in think           # 결정론 재서술은 없다


@pytest.mark.asyncio
async def test_thinking_no_canned_text_on_gap_answer() -> None:
    # 근거 0건이어도 thinking 에 결정론 gap 텍스트를 싣지 않는다(근거 유무는 N4 모델
    # 답변/CoT 가 EVIDENCE GAP 블록으로 전달).
    with tempfile.TemporaryDirectory() as tmp:
        runner = _build(Path(tmp), _script(), retriever=_EmptyRetriever())
        final, think = await _collect_reasoning_stream(runner, _req())
        assert final is not None and final.refusal_reason is None
        for canned in _CANNED_THINKING:
            assert canned not in think
        assert "gap-answer" not in think and "근거 0건" not in think


@pytest.mark.asyncio
async def test_thinking_surfaces_model_rationale_on_general_route() -> None:
    # general 우회도 N0 모델 rationale 을 보이고, 결정론 텍스트는 없다.
    with tempfile.TemporaryDirectory() as tmp:
        llm = _ScriptLLM(gen_texts=[_TRIAGE_GENERAL], stream_text=_GENERAL_ANSWER)
        runner = _build(Path(tmp), llm, retriever=_SpyRetriever())
        _, think = await _collect_reasoning_stream(runner, _req_general())
        assert "일반 개념 — 추론 가능" in think    # 모델 산출(rationale)
        for canned in _CANNED_THINKING:
            assert canned not in think
