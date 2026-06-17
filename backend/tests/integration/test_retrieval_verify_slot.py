"""retrieval.verify_slot — 실 vLLM 통합 테스트(spec_driven_v2 Phase 2).

검증은 swap 후 Node1(main = utility_llm) 업무지만, 이 테스트는 verify 도구가 실 vLLM 에서
도는지만 단독 확인하므로 별도 엔드포인트 vLLM 을 주입한다(엔드포인트 분리 ≠ 논리 노드 분리).
환경변수로 엔드포인트를 받아 실제 `HttpLLM` 을 만들고, 미설정 시 모듈 전체 skip(opt-in).

  SPEC_DRIVEN_V2_NODE2_ENDPOINT — verify 용 vLLM OpenAI-compat 엔드포인트
                                  (예: http://192.168.100.11:8001/v1)
  SPEC_DRIVEN_V2_NODE2_MODEL    — verify 모델 id (예: gemma-4-26b-a4b-it)

검증: 실 vLLM 응답으로 verify 가 (a) 입력 chunk_id 의 부분집합만 산출(환각 id 차단),
(b) guided_json 파싱 성공·method=="llm", (c) 잘못된 엔드포인트 → method=="fallback"·
전량 necessary(안전 degrade).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.adapters.llm.http import HttpLLM
from app.adapters.slot_verifier_llm import SlotVerifierLlm
from app.adapters.tools.retrieval_verify_slot import RetrievalVerifySlotTool
from app.application.prompting.spec_driven_source import SpecDrivenVerifySource
from app.domain.retrieval import RetrievedChunk, VerifySlotInput, VerifySlotResult
from app.ports.tool import ToolExecutionContext

pytestmark = pytest.mark.integration

_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


def _node2_endpoint() -> str | None:
    return os.environ.get("SPEC_DRIVEN_V2_NODE2_ENDPOINT")


@pytest.fixture(scope="session")
def verify_source() -> SpecDrivenVerifySource:
    return SpecDrivenVerifySource(_REPO_PROMPTS)


@pytest.fixture(scope="session")
def node2_llm() -> HttpLLM:
    ep = _node2_endpoint()
    if not ep:
        pytest.skip(
            "SPEC_DRIVEN_V2_NODE2_ENDPOINT not set; verify_slot integration skipped"
        )
    model = os.environ.get("SPEC_DRIVEN_V2_NODE2_MODEL", "gemma-4-26b-a4b-it")
    return HttpLLM(provider="openai_compat", endpoint=ep, model=model,
                   timeout_s=120.0, max_attempts=2)


def _chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id="10cfr50.46#a#1", document_id="10cfr50.46", score=0.9,
            snippet="ECCS shall be designed so that the peak cladding temperature "
                    "does not exceed 2200 F under any postulated LOCA. See RG 1.157 "
                    "for best-estimate methods.",
        ),
        RetrievedChunk(
            chunk_id="toc#0", document_id="10cfr50", score=0.4,
            snippet="Table of Contents — Part 50 Domestic Licensing of Production "
                    "and Utilization Facilities.",
        ),
    ]


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="v2-verify-it", trace_id="t",
        app_profile="local", agent_variant="spec_driven_v2",
    )


@pytest.mark.asyncio
async def test_verify_subset_and_method_llm(node2_llm, verify_source) -> None:
    tool = RetrievalVerifySlotTool(
        slot_verifier=SlotVerifierLlm(llm=node2_llm, source=verify_source),
        max_concurrency=2,
    )
    chunks = _chunks()
    res = await tool.invoke(
        VerifySlotInput(
            query_text="10 CFR 50.46 ECCS 요건의 PCT 한계는?",
            answer_spec="intent: compliance\nrequired_slots:\n- governing_clause [criterion]",
            slot_name="governing_clause",
            slot_query="10 CFR 50.46 ECCS peak cladding temperature limit",
            chunks=chunks,
        ),
        _ctx(),
    )
    assert res.status == "success"
    out = VerifySlotResult.model_validate(res.output)
    assert out.method == "llm"
    valid = {c.chunk_id for c in chunks}
    # 환각 id 차단 — 출력은 입력 chunk_id 의 부분집합.
    assert set(out.necessary_chunk_ids) <= valid
    assert set(out.multihop_chunk_ids) <= valid


@pytest.mark.asyncio
async def test_verify_fallback_on_unavailable(verify_source) -> None:
    # 잘못된 엔드포인트 → LLMUnavailableError → 안전 degrade(전량 necessary, 멀티홉 없음).
    bad = HttpLLM(provider="openai_compat",
                  endpoint="http://127.0.0.1:9/v1", model="x",
                  timeout_s=2.0, max_attempts=1)
    tool = RetrievalVerifySlotTool(
        slot_verifier=SlotVerifierLlm(llm=bad, source=verify_source),
    )
    chunks = _chunks()
    res = await tool.invoke(
        VerifySlotInput(
            query_text="q", answer_spec="spec", slot_name="s",
            slot_query="sq", chunks=chunks,
        ),
        _ctx(),
    )
    out = VerifySlotResult.model_validate(res.output)
    assert out.method == "fallback"
    assert set(out.necessary_chunk_ids) == {c.chunk_id for c in chunks}
    assert out.multihop_chunk_ids == []
