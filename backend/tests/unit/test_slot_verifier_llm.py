"""SlotVerifierLlm — 청크별 fan-out 검증 단위 테스트(spec_driven_v2 Node2).

검증이 청크마다 독립 LLM 호출을 asyncio.gather 로 동시 발사하고(continuous batch), boolean
판정을 모아 necessary/multihop 식별자 리스트로 집계하는지 — vLLM 컨테이너 없이 fake LLMPort
+ duck-typed source 로(원칙: tests use fake ports). 동시성 캡(semaphore)·청크별 실패 보존·
전 청크 실패 fallback 도 함께 검증한다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.slot_verifier_llm import SlotVerifierLlm
from app.ports.llm import ChatMessage, GrammarSpec, LLMResult, LLMUnavailableError


class _FakeSource:
    """SlotVerifierLlm 이 읽는 source 계약(prompt_body/schema/model_options)만 갖춘 duck."""

    prompt_body = "system verify chunk"
    schema = {"type": "object"}
    model_options = {"temperature": 0.0}


class _ScriptedLLM:
    """chunk_id → {necessary, multihop, rationale} 매핑으로 응답하는 fake LLMPort.

    chunk_id 가 fail 집합에 있으면 LLMUnavailableError 를 던진다. user 메시지에서 `[<cid>]`
    를 뽑아 어떤 청크 호출인지 식별한다. 동시 호출 수(in-flight)의 관측 최댓값을 기록한다."""

    def __init__(self, verdicts: dict, *, fail: set[str] | None = None) -> None:
        self._verdicts = verdicts
        self._fail = fail or set()
        self.calls = 0
        self._inflight = 0
        self.max_inflight = 0

    @property
    def model_id(self) -> str:
        return "scripted"

    async def generate_messages(self, messages, *, model_options=None, grammar=None):
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            self.calls += 1
            # user 메시지에서 `[<cid>]` 추출(THE CHUNK 라인의 id).
            user = next(m.content for m in messages if m.role == "user")
            cid = user.split("[", 1)[1].split("]", 1)[0]
            if cid in self._fail:
                raise LLMUnavailableError(f"node2 down for {cid}")
            # 동시성 관측이 의미 있도록 한 틱 양보.
            await asyncio.sleep(0.01)
            return LLMResult(
                text=json.dumps(self._verdicts[cid]),
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                model_id=self.model_id,
            )
        finally:
            self._inflight -= 1


def _chunks(*ids: str) -> list[dict]:
    return [{"chunk_id": c, "document_id": "d", "snippet": f"body {c}"} for c in ids]


async def _verify(verifier: SlotVerifierLlm, chunks: list[dict]) -> dict:
    return await verifier.verify_slot(
        query_text="q", answer_spec="spec", slot_name="s",
        slot_query="sq", chunks=chunks,
    )


@pytest.mark.asyncio
async def test_per_chunk_fanout_and_aggregation() -> None:
    llm = _ScriptedLLM({
        "c1": {"necessary": True, "multihop": True, "rationale": "on-point + cites RG"},
        "c2": {"necessary": True, "multihop": False, "rationale": "definition"},
        "c3": {"necessary": False, "multihop": False, "rationale": "toc noise"},
    })
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2", "c3"))

    # 청크마다 1회씩 호출(슬롯 일괄 1회가 아니라 fan-out).
    assert llm.calls == 3
    assert out["method"] == "llm"
    assert out["necessary_chunk_ids"] == ["c1", "c2"]
    assert out["multihop_chunk_ids"] == ["c1"]
    # rationale 은 청크별로 줄바꿈 결합(c1·c2·c3 각각 한 줄).
    assert out["rationale"].count("\n") == 2
    assert "[c1]" in out["rationale"] and "[c3]" in out["rationale"]


@pytest.mark.asyncio
async def test_per_chunk_failure_preserves_as_necessary() -> None:
    # c2 만 Node2 미가용 → 그 청크는 necessary 로 보존(전량 보존 degrade), 멀티홉 제외.
    # 나머지 성공이므로 method 는 "llm".
    llm = _ScriptedLLM(
        {
            "c1": {"necessary": True, "multihop": False, "rationale": "x"},
            "c3": {"necessary": False, "multihop": True, "rationale": "y"},
        },
        fail={"c2"},
    )
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2", "c3"))

    assert out["method"] == "llm"
    assert "c2" in out["necessary_chunk_ids"]      # 실패 → 보존
    assert "c2" not in out["multihop_chunk_ids"]   # 실패 → 멀티홉 제외
    assert out["multihop_chunk_ids"] == ["c3"]


@pytest.mark.asyncio
async def test_all_chunks_fail_is_fallback() -> None:
    # 전 청크 실패 → 기존 fallback 계약과 byte-identical(necessary=전량, multihop=없음).
    llm = _ScriptedLLM({}, fail={"c1", "c2"})
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2"))

    assert out["method"] == "fallback"
    assert set(out["necessary_chunk_ids"]) == {"c1", "c2"}
    assert out["multihop_chunk_ids"] == []


@pytest.mark.asyncio
async def test_concurrency_cap_is_respected() -> None:
    # 청크 5개·캡 2 → 동시 in-flight 가 2 를 넘지 않아야 한다(전역 semaphore).
    verdicts = {
        f"c{i}": {"necessary": True, "multihop": False, "rationale": "x"}
        for i in range(5)
    }
    llm = _ScriptedLLM(verdicts)
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource(), max_concurrency=2)
    out = await _verify(verifier, _chunks(*verdicts.keys()))

    assert llm.calls == 5
    assert llm.max_inflight <= 2
    assert len(out["necessary_chunk_ids"]) == 5


@pytest.mark.asyncio
async def test_empty_chunks_is_fallback_noop() -> None:
    llm = _ScriptedLLM({})
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, [])

    assert llm.calls == 0
    assert out == {"necessary_chunk_ids": [], "multihop_chunk_ids": [],
                   "rationale": "", "method": "fallback"}
