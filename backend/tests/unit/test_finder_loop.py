from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeToolLLM
from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.retrieval_scope import RetrievalScopeTool
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.submit_verdict import SubmitVerdictTool
from app.application.agents.finder_loop import (
    FINDER_TOOL_SPECS,
    run_finder,
    tools_schema_hash,
)
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.finder import AnswerSlot, AnswerSpec
from app.ports.llm import LLMToolResult, ToolCall
from app.ports.tool import ToolExecutionContext

# F-4 — Finder agentic 루프. 종료 단위 = (verdict | research_rounds≥recover_limit |
# max_turns backstop)지 raw 턴이 아니다(두 문서가 반복 경고). 세 종료 경로를 각각
# 테스트하고, FinderRound 가 검색 라운드당 1건 확정되는지 본다.

_CTX = ToolExecutionContext(
    interaction_id="i", trace_id="", app_profile="local",
    agent_variant="agentic_finder_v4", scenario_object="O4", scenario_depth="D2",
)
_SPEC = AnswerSpec(
    required_slots=(AnswerSlot(name="governing_clause"), AnswerSlot(name="requirement_text")),
    depth="D2", instantiation_method="llm",
)


def _executor(tmp: Path) -> ToolExecutor:
    body = {"tools": {
        "retrieval.search": {"version": "v1", "adapter": "reranked", "timeout_ms": 6000, "retry": 0, "required": False},
        "retrieval.scope": {"version": "v1", "adapter": "corpus_map", "timeout_ms": 1000, "retry": 0, "required": False},
        "submit_verdict": {"version": "v1", "adapter": "local", "timeout_ms": 1000, "retry": 0, "required": False},
    }}
    p = tmp / "tools.yaml"
    p.write_text(yaml.safe_dump(body))
    registry = ToolRegistry.from_yaml(p)
    sink = FilesystemEventSink(root=str(tmp / "ev"), prefix="t")
    tools = {
        "retrieval.search": RetrievalSearchTool(retriever=LocalRetrieverTool(), reranker=IdentityReranker()),
        "retrieval.scope": RetrievalScopeTool(),
        "submit_verdict": SubmitVerdictTool(),
    }
    return ToolExecutor(registry=registry, tools=tools, event_sink=sink)


def _r(*calls: ToolCall, text: str = "", stop: str = "tool_calls") -> LLMToolResult:
    return LLMToolResult(text=text, tool_calls=tuple(calls), stop_reason=stop,
                         token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id="fake-tool")


def _scope() -> ToolCall:
    return ToolCall("c-scope", "retrieval.scope", {})


def _search(q="i-SMR ECCS", top_k=3) -> ToolCall:
    return ToolCall("c-search", "retrieval.search", {"query_text": q, "top_k": top_k})


def _verdict(sufficient=True, missing=None, reason="ok") -> ToolCall:
    return ToolCall("c-verdict", "submit_verdict",
                    {"sufficient": sufficient, "missing_slots": missing or [], "reason": reason})


async def _run(llm, ex, *, recover_limit=3, max_turns=10):
    return await run_finder(
        llm=llm, tool_executor=ex, ctx=_CTX,
        system_prompt_body="finder 지시", finder_policy_hash="pol16",
        query_text="i-SMR ECCS 요건", answer_spec=_SPEC, record=lambda r: None,
        recover_limit=recover_limit, max_turns=max_turns,
    )


@pytest.mark.asyncio
async def test_exit_on_submit_verdict() -> None:
    # scope → search → verdict. 직렬(1턴 1도구). verdict 캡처 후 종료. 용어 정규화는
    # N1.5 conductor(terminology.canonicalize)로 상향 — Finder 루프엔 normalize 없음.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(_scope()),
            _r(_search()),
            _r(_verdict(sufficient=True, reason="슬롯 충족")),
        ])
        result = await _run(llm, _executor(Path(tmp)))
        assert result.verdict["sufficient"] is True
        assert result.recover_limit_hit is False
        assert len(result.chunks) >= 1            # 검색 chunk 누적.
        assert len(result.finder_rounds) == 1     # 검색 라운드 1건.
        rnd = result.finder_rounds[0]
        assert rnd.num_chunks >= 1
        assert rnd.normalized_terms == ()         # 정규화는 라운드 단위 아님(N1.5 conductor).
        assert rnd.scope_params.get("mode") == "off"  # CorpusMap.default → off.
        assert rnd.reranker_score_dist            # 점수 분포 계측.
        assert rnd.verdict_sufficient is True      # verdict 가 직전 검색 라운드에 귀속.


@pytest.mark.asyncio
async def test_exit_on_recover_limit() -> None:
    # verdict 를 안 내고 계속 재검색 → recover_limit 소진. off-by-one 핀:
    # research_rounds 는 *재*검색에만 증가하므로 recover_limit=2 는 3번째 검색에서 종료.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[_r(_search())])  # 매 턴 검색(소진 시 반복).
        result = await _run(llm, _executor(Path(tmp)), recover_limit=2, max_turns=20)
        assert result.recover_limit_hit is True
        assert result.verdict["sufficient"] is False   # synthetic verdict.
        assert len(result.finder_rounds) == 3          # 1 초기 + 2 재검색 = 3 검색.
        # 마지막 라운드에 synthetic verdict 귀속.
        assert result.finder_rounds[-1].verdict_sufficient is False
        assert "recover_limit" in result.finder_rounds[-1].verdict_reason


@pytest.mark.asyncio
async def test_exit_on_max_turns_backstop_when_no_tool_calls() -> None:
    # 모델이 도구를 안 부른다(tools 미지원, §9) → 재검색 미발생 → recover_limit 안 걸림
    # → max_turns 가 무한 루프 backstop. 검색이 없으니 chunks·finder_rounds 비어 있다.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[_r(stop="stop")])  # 빈 tool_calls.
        result = await _run(llm, _executor(Path(tmp)), recover_limit=3, max_turns=4)
        assert result.turns_used == 4
        assert result.recover_limit_hit is False
        assert result.verdict["sufficient"] is False
        assert "max_turns" in result.verdict["reason"]
        assert result.chunks == []
        assert result.finder_rounds == []


@pytest.mark.asyncio
async def test_tool_calls_routed_through_executor_records() -> None:
    # "도구는 통제된다" — scope/search/submit_verdict 모두 ToolExecutor 경로로
    # record 된다(submit_verdict 도 no-op tool 로 동일 기록).
    with tempfile.TemporaryDirectory() as tmp:
        recorded: list[str] = []
        llm = FakeToolLLM(script=[_r(_scope()), _r(_search()), _r(_verdict())])
        await run_finder(
            llm=llm, tool_executor=_executor(Path(tmp)), ctx=_CTX,
            system_prompt_body="x", finder_policy_hash="p",
            query_text="q", answer_spec=_SPEC,
            record=lambda r: recorded.append(r.tool_name),
            recover_limit=3, max_turns=10,
        )
        assert recorded == ["retrieval.scope", "retrieval.search", "submit_verdict"]


@pytest.mark.asyncio
async def test_search_failure_is_fed_back_not_raised() -> None:
    # retrieval.search 실패(query_text 누락 → 검증 실패)는 예외로 루프를 죽이지 않고
    # tool_result(is_error)로 LLM 에 되먹여진다(llm_tool_calling §9). retrieval.search
    # 는 required:false 라 ToolExecutor 가 RequiredToolFailed 대신 failed 결과를 돌려준다.
    with tempfile.TemporaryDirectory() as tmp:
        llm = FakeToolLLM(script=[
            _r(ToolCall("c-bad", "retrieval.search", {})),  # query_text 누락.
            _r(_verdict(sufficient=False, missing=["governing_clause"], reason="검색 실패")),
        ])
        # 예외 없이 완주해야 한다.
        result = await _run(llm, _executor(Path(tmp)))
        assert result.verdict["sufficient"] is False
        assert result.chunks == []                  # 실패 검색 → chunk 없음.
        assert len(result.finder_rounds) == 1        # 실패도 검색 라운드 1건(num_chunks=0).
        assert result.finder_rounds[0].num_chunks == 0


def test_tools_schema_hash_is_stable_and_covers_finder_tools() -> None:
    # 용어 정규화 상향(N1.5)으로 Finder 도구 set 은 scope/search/submit_verdict 3종.
    # 검색범위 확장(terminology.expand)은 P3 에서 recover 전용으로 추가 예정.
    assert {t.name for t in FINDER_TOOL_SPECS} == {
        "retrieval.scope", "retrieval.search", "submit_verdict"}
    assert tools_schema_hash() == tools_schema_hash()  # 결정론(재현 핀).
    assert len(tools_schema_hash()) == 16
