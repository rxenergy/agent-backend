from __future__ import annotations

import json

import pytest
from structlog.testing import capture_logs

from app.adapters.ref_postprocess.ref_extractor_rule import (
    RESOLVE_WITH_FOLLOW_UP_SCHEMA,
    extract_refs_with_follow_up,
    resolve_text_with_follow_up,
)
from app.adapters.ref_postprocess.ref_resolver import Candidate, ResolvedRef
from app.adapters.ref_postprocess.settings import RefSettings
from app.adapters.tools.retrieval_follow_up import RetrievalFollowUpTool
from app.domain.retrieval import FollowUpInput, RetrievedChunk
from app.ports.llm import ChatMessage, GrammarSpec, LLMResult
from app.ports.tool import ToolExecutionContext

# ref 추출이 openai SDK 가 아니라 LLMPort.generate_messages(async + json_schema grammar)
# 위에서 동작하고, RetrievalFollowUpTool 이 to_thread 없이 async 추출기를 직접 await
# 하는지 검증한다 — vLLM 컨테이너 없이 fake 포트만으로(원칙: tests use fake ports).

_CTX = ToolExecutionContext(
    interaction_id="i", trace_id="t", app_profile="local",
    agent_variant="agentic_finder_v4",
)


class _SchemaEchoLLM:
    """generate_messages 호출을 기록하고 고정 JSON(스키마 부합)을 돌려주는 fake LLMPort.

    `last_grammar`/`last_messages` 로 호출자가 system+user 메시지 + json_schema
    guided decoding 을 단언할 수 있게 노출한다."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0
        self.last_grammar: GrammarSpec | None = None
        self.last_messages: list[ChatMessage] = []

    @property
    def model_id(self) -> str:
        return "schema-echo"

    async def generate_messages(self, messages, *, model_options=None, grammar=None):
        self.calls += 1
        self.last_grammar = grammar
        self.last_messages = list(messages)
        return LLMResult(
            text=json.dumps(self._payload),
            token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            model_id=self.model_id,
        )


@pytest.mark.asyncio
async def test_extract_refs_with_follow_up_uses_llmport_and_json_schema_grammar() -> None:
    # 방안 B — follow_up_query 는 reference 에 inline nested(평행 배열·target_identifiers 폐기).
    llm = _SchemaEchoLLM({
        "references": [
            {"raw_citation": "RG 1.68", "kind": "RG", "identifier": "RG 1.68",
             "section_path": [],
             "follow_up_query": {"query_text": "testing acceptance criteria",
                                 "intent": "acceptance"}},
        ],
    })

    raw_refs = await extract_refs_with_follow_up(
        query_text="what are the testing criteria?",
        chunk_text="see RG 1.68 for details",
        settings=RefSettings.from_env(),
        llm=llm,
    )

    # LLMPort 경로로 1회 호출 + json_schema grammar 전달 + system/user 메시지 구성.
    assert llm.calls == 1
    assert llm.last_grammar == GrammarSpec(
        kind="json_schema", value=RESOLVE_WITH_FOLLOW_UP_SCHEMA
    )
    assert [m.role for m in llm.last_messages] == ["system", "user"]
    # 응답 파싱이 그대로 동작 — follow_up_query 가 그 reference 에 실린다.
    assert [r.identifier for r in raw_refs] == ["RG 1.68"]
    assert raw_refs[0].follow_up_query == {
        "query_text": "testing acceptance criteria", "intent": "acceptance",
    }


class _FakeRefExtractor:
    """async RefExtractorPort fake — 청크별 고정 follow-up 을 돌려주고 동시 진입을
    기록한다(세마포어 캡 검증용)."""

    def __init__(self) -> None:
        self.seen_chunks: list[str] = []
        self.seen_kwargs: list[dict] = []

    async def extract_follow_ups(self, query_text, chunk_text,
                                 current_source_id=None, min_score=0.6,
                                 answer_spec=None, slot_query=None,
                                 necessity_only=False, search_direction=None):
        self.seen_chunks.append(chunk_text)
        self.seen_kwargs.append({"answer_spec": answer_spec,
                                 "slot_query": slot_query,
                                 "necessity_only": necessity_only,
                                 "search_direction": search_direction})
        # 두 청크가 같은 query_text 를 내도록 해 dedup 도 함께 검증.
        return [{"query_text": "shared-query",
                 "target_source_ids": [current_source_id or "s"],
                 "intent": ""}]


@pytest.mark.asyncio
async def test_follow_up_tool_awaits_async_extractor_and_dedupes() -> None:
    extractor = _FakeRefExtractor()
    tool = RetrievalFollowUpTool(ref_extractor=extractor, max_concurrency=2)

    tool_input = FollowUpInput(
        query_text="q",
        chunks=[
            RetrievedChunk(chunk_id="c1", document_id="d", score=0.9,
                           snippet="RG 1.68", source_id="s1"),
            RetrievedChunk(chunk_id="c2", document_id="d", score=0.8,
                           snippet="RG 1.70", source_id="s2"),
        ],
    )

    result = await tool.invoke(tool_input, _CTX)

    assert result.status == "success"
    # 두 청크 모두 추출기로 전달됐고(async 직접 await), 동일 query_text 는 1개로 dedup.
    assert len(extractor.seen_chunks) == 2
    queries = result.output["follow_up_queries"]
    assert len(queries) == 1
    assert queries[0]["query_text"] == "shared-query"
    # 하위호환 — 새 필드를 안 넘기면 추출기에 v1 기본값(None/None/False/None)이 전달된다.
    assert extractor.seen_kwargs[0] == {
        "answer_spec": None, "slot_query": None, "necessity_only": False,
        "search_direction": None,
    }


@pytest.mark.asyncio
async def test_follow_up_tool_passes_necessity_inputs_to_extractor() -> None:
    # spec_driven_v2 — answer_spec/slot_query/necessity_only 가 추출기까지 전달되는지 검증.
    extractor = _FakeRefExtractor()
    tool = RetrievalFollowUpTool(ref_extractor=extractor, max_concurrency=2)
    tool_input = FollowUpInput(
        query_text="q",
        chunks=[RetrievedChunk(chunk_id="c1", document_id="d", score=0.9,
                               snippet="RG 1.68", source_id="s1")],
        answer_spec="intent: compliance\nrequired_slots:\n- governing_clause",
        slot_query="10 CFR 50.46 ECCS",
        necessity_only=True,
    )
    result = await tool.invoke(tool_input, _CTX)
    assert result.status == "success"
    assert extractor.seen_kwargs[0] == {
        "answer_spec": "intent: compliance\nrequired_slots:\n- governing_clause",
        "slot_query": "10 CFR 50.46 ECCS",
        "necessity_only": True,
        "search_direction": None,
    }


@pytest.mark.asyncio
async def test_follow_up_tool_passes_per_chunk_search_direction() -> None:
    # verify 의 search_directions(chunk_id → 방향)가 청크별로 추출기에 전달되는지 검증.
    extractor = _FakeRefExtractor()
    tool = RetrievalFollowUpTool(ref_extractor=extractor, max_concurrency=2)
    tool_input = FollowUpInput(
        query_text="q",
        chunks=[
            RetrievedChunk(chunk_id="c1", document_id="d", score=0.9,
                           snippet="RG 1.68", source_id="s1"),
            RetrievedChunk(chunk_id="c2", document_id="d", score=0.8,
                           snippet="RG 1.70", source_id="s2"),
        ],
        necessity_only=True,
        search_directions={"c1": "find the acceptance criteria in RG 1.68"},
    )
    result = await tool.invoke(tool_input, _CTX)
    assert result.status == "success"
    # c1 은 방향이 전달되고, 방향 없는 c2 는 None(기존 동작).
    by_dir = {kw["search_direction"] for kw in extractor.seen_kwargs}
    assert "find the acceptance criteria in RG 1.68" in by_dir
    assert None in by_dir


@pytest.mark.asyncio
async def test_necessity_prompt_includes_search_direction_block() -> None:
    # search_direction 이 주어지면 user content 에 SEARCH DIRECTION 블록이 실린다.
    llm = _SchemaEchoLLM({"references": []})
    await extract_refs_with_follow_up(
        query_text="q", chunk_text="see RG 1.68",
        settings=RefSettings.from_env(), llm=llm,
        answer_spec="SPEC", slot_query="SLOT", necessity_only=True,
        search_direction="DIRECTION-X",
    )
    user_msg = llm.last_messages[1].content
    assert "SEARCH DIRECTION: DIRECTION-X" in user_msg


@pytest.mark.asyncio
async def test_necessity_mode_uses_necessity_system_prompt() -> None:
    # necessity_only=True 면 SYSTEM_PROMPT_NECESSITY + ANSWER SPEC/SLOT 블록을 싣고,
    # 미지정 시 기존 SYSTEM_PROMPT_WITH_FOLLOW_UP 를 쓴다(하위호환).
    from app.adapters.ref_postprocess.ref_extractor_rule import (
        SYSTEM_PROMPT_NECESSITY,
        SYSTEM_PROMPT_WITH_FOLLOW_UP,
    )

    llm = _SchemaEchoLLM({"references": []})
    await extract_refs_with_follow_up(
        query_text="q", chunk_text="see RG 1.68",
        settings=RefSettings.from_env(), llm=llm,
        answer_spec="SPEC-BLOCK", slot_query="SLOT-Q", necessity_only=True,
    )
    sys_msg = llm.last_messages[0].content
    user_msg = llm.last_messages[1].content
    assert sys_msg == SYSTEM_PROMPT_NECESSITY
    assert "ANSWER SPEC:" in user_msg and "SPEC-BLOCK" in user_msg
    assert "SLOT SEARCH QUERY: SLOT-Q" in user_msg

    llm2 = _SchemaEchoLLM({"references": []})
    await extract_refs_with_follow_up(
        query_text="q", chunk_text="see RG 1.68",
        settings=RefSettings.from_env(), llm=llm2,
    )
    assert llm2.last_messages[0].content == SYSTEM_PROMPT_WITH_FOLLOW_UP
    assert "ORIGINAL USER QUERY:" in llm2.last_messages[1].content


# ---------------------------------------------------------------------------
# 방안 B — per-reference resolve walk (resolve_text_with_follow_up)
# reference 가 inline 으로 소유한 follow_up_query 가 그 reference 의 해소 source_id 와
# 결합되는지(cross-array join 없이), 미해소 시 drop+로그되는지 검증.
# ---------------------------------------------------------------------------

class _StubResolver:
    """RefResolver 대역 — RawRef → 미리 지정한 후보로 해소한다. resolve_many 는
    입력 순서를 보존(production 불변식과 동일)해 zip 인덱스 정렬을 유지한다."""

    def __init__(self, by_identifier: dict[str, list[Candidate]]) -> None:
        self._by_identifier = by_identifier

    def resolve_many(self, raw_refs):
        return [
            ResolvedRef(
                raw_citation=r.raw_citation, kind=r.kind,
                candidates=self._by_identifier.get(r.identifier, []),
            )
            for r in raw_refs
        ]


@pytest.mark.asyncio
async def test_nested_follow_up_resolves_to_owning_reference_source_ids() -> None:
    # (a) reference 가 inline 으로 가진 follow_up_query → 그 reference 의 해소 source_id 결합.
    llm = _SchemaEchoLLM({
        "references": [
            {"raw_citation": "RG 1.68", "kind": "RG", "identifier": "RG 1.68",
             "section_path": [],
             "follow_up_query": {"query_text": "acceptance criteria",
                                 "intent": "acceptance"}},
        ],
    })
    resolver = _StubResolver({
        "RG 1.68": [Candidate(source_id="src-1", score=0.9, matched_on="x")],
    })
    out = await resolve_text_with_follow_up(
        query_text="q", chunk_text="see RG 1.68",
        resolver=resolver, settings=RefSettings.from_env(), llm=llm,
        necessity_only=True,
    )
    fqs = out["follow_up_queries"]
    assert len(fqs) == 1
    assert fqs[0].query_text == "acceptance criteria"
    assert fqs[0].target_source_ids == ["src-1"]
    assert fqs[0].intent == "acceptance"


@pytest.mark.asyncio
async def test_reference_without_follow_up_yields_no_query_and_no_drop_log() -> None:
    # (b) follow_up_query 부재 → follow-up 없음, drop 로그도 *미발생*(부재 ≠ drop).
    llm = _SchemaEchoLLM({
        "references": [
            {"raw_citation": "RG 1.68", "kind": "RG", "identifier": "RG 1.68",
             "section_path": []},
        ],
    })
    resolver = _StubResolver({
        "RG 1.68": [Candidate(source_id="src-1", score=0.9, matched_on="x")],
    })
    with capture_logs() as logs:
        out = await resolve_text_with_follow_up(
            query_text="q", chunk_text="see RG 1.68",
            resolver=resolver, settings=RefSettings.from_env(), llm=llm,
            necessity_only=True,
        )
    assert out["follow_up_queries"] == []
    assert not [e for e in logs if e.get("event") == "follow_up_dropped_unresolved"]


@pytest.mark.asyncio
async def test_unresolved_reference_drops_follow_up_and_logs() -> None:
    # (c) follow_up_query 있으나 후보가 min_score 미만 → drop + follow_up_dropped_unresolved 로그.
    llm = _SchemaEchoLLM({
        "references": [
            {"raw_citation": "RG 9.99", "kind": "RG", "identifier": "RG 9.99",
             "section_path": [],
             "follow_up_query": {"query_text": "orphan query", "intent": ""}},
        ],
    })
    resolver = _StubResolver({
        "RG 9.99": [Candidate(source_id="src-low", score=0.3, matched_on="weak")],
    })
    with capture_logs() as logs:
        out = await resolve_text_with_follow_up(
            query_text="q", chunk_text="see RG 9.99",
            resolver=resolver, settings=RefSettings.from_env(), llm=llm,
            min_score=0.6, necessity_only=True,
        )
    assert out["follow_up_queries"] == []
    drops = [e for e in logs if e.get("event") == "follow_up_dropped_unresolved"]
    assert len(drops) == 1
    assert drops[0]["dropped"] == 1
