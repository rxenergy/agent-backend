from __future__ import annotations

import json

import pytest

from app.adapters.ref_postprocess.ref_extractor_rule import (
    RESOLVE_WITH_FOLLOW_UP_SCHEMA,
    extract_refs_with_follow_up,
)
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
    llm = _SchemaEchoLLM({
        "references": [
            {"raw_citation": "RG 1.68", "kind": "RG", "identifier": "RG 1.68",
             "section_path": []},
        ],
        "follow_up_queries": [
            {"query_text": "testing acceptance criteria",
             "target_identifiers": ["RG 1.68"], "intent": "acceptance"},
        ],
    })

    raw_refs, raw_follow_ups = await extract_refs_with_follow_up(
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
    # 응답 파싱이 그대로 동작.
    assert [r.identifier for r in raw_refs] == ["RG 1.68"]
    assert raw_follow_ups[0]["query_text"] == "testing acceptance criteria"
    assert raw_follow_ups[0]["target_identifiers"] == ["RG 1.68"]


class _FakeRefExtractor:
    """async RefExtractorPort fake — 청크별 고정 follow-up 을 돌려주고 동시 진입을
    기록한다(세마포어 캡 검증용)."""

    def __init__(self) -> None:
        self.seen_chunks: list[str] = []

    async def extract_follow_ups(self, query_text, chunk_text,
                                 current_source_id=None, min_score=0.6):
        self.seen_chunks.append(chunk_text)
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
