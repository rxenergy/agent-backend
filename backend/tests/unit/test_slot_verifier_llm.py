"""SlotVerifierLlm — 슬롯당 단일 호출 검증 단위 테스트(spec_driven_v2 Node2 = sub).

검증이 슬롯 1개의 청크 전체를 한 프롬프트로 합쳐 **단일 LLM 호출**로 판정하고, 모델이 낸
necessary/multihop 식별자 리스트를 입력 부분집합으로 필터하는지 — vLLM 컨테이너 없이 fake
LLMPort + duck-typed source 로(원칙: tests use fake ports). LLM 미가용/파싱 실패 시 슬롯
단위 fallback(전량 보존 + 실패 사유 rationale)·동시 슬롯 캡(semaphore)도 함께 검증한다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.slot_verifier_llm import SlotVerifierLlm
from app.ports.llm import LLMResult, LLMUnavailableError


class _FakeSource:
    """SlotVerifierLlm 이 읽는 source 계약(prompt_body/schema/model_options)만 갖춘 duck."""

    prompt_body = "system verify slot"
    schema = {"type": "object"}
    model_options = {"temperature": 0.0}


class _ScriptedLLM:
    """슬롯 단위 JSON(`{rationale, necessary_chunk_ids, multihop_chunk_ids}`) 1개를 반환하는
    fake LLMPort. `fail=True` 면 LLMUnavailableError, `raw` 가 주어지면 그 문자열을 그대로
    반환(파싱 실패 케이스). 동시 호출 수(in-flight)의 관측 최댓값을 기록한다(동시 슬롯 캡 검증)."""

    def __init__(self, verdict: dict | None = None, *, fail: bool = False,
                 raw: str | None = None) -> None:
        self._verdict = verdict or {}
        self._fail = fail
        self._raw = raw
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
            if self._fail:
                raise LLMUnavailableError("node1 down")
            # 동시성 관측이 의미 있도록 한 틱 양보.
            await asyncio.sleep(0.01)
            text = self._raw if self._raw is not None else json.dumps(self._verdict)
            return LLMResult(
                text=text,
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
async def test_per_slot_single_call_and_filter() -> None:
    # 슬롯의 청크 전체가 한 번의 호출로 판정되고, 모델의 id 리스트가 그대로 반영된다.
    llm = _ScriptedLLM({
        "rationale": "c1 cites RG and is on-point; c2 is the definition; c3 toc noise",
        "necessary_chunk_ids": ["c1", "c2"],
        "multihop_chunk_ids": ["c1"],
    })
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2", "c3"))

    # 슬롯당 1회 호출(청크별 fan-out 아님).
    assert llm.calls == 1
    assert out["method"] == "llm"
    assert out["necessary_chunk_ids"] == ["c1", "c2"]
    assert out["multihop_chunk_ids"] == ["c1"]
    # rationale 은 모델의 단일 문자열(청크별 결합 아님).
    assert out["rationale"].startswith("c1 cites RG")


@pytest.mark.asyncio
async def test_hallucinated_ids_filtered() -> None:
    # 모델이 입력에 없는 id(ghost)를 내면 부분집합 필터로 제거된다(계약: 두 집합 ⊆ 입력).
    llm = _ScriptedLLM({
        "rationale": "keeps c1, hallucinates ghost",
        "necessary_chunk_ids": ["c1", "ghost"],
        "multihop_chunk_ids": ["ghost"],
    })
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2"))

    assert out["method"] == "llm"
    assert out["necessary_chunk_ids"] == ["c1"]   # ghost 제거
    assert "ghost" not in out["multihop_chunk_ids"]
    assert out["multihop_chunk_ids"] == []


@pytest.mark.asyncio
async def test_empty_necessary_is_not_fallback() -> None:
    # 성공 호출이 necessary 를 비워 내는 것은 *유효*(프롬프트 허용) — fallback 으로 바꾸지 않는다.
    llm = _ScriptedLLM({
        "rationale": "none of the chunks help",
        "necessary_chunk_ids": [],
        "multihop_chunk_ids": [],
    })
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2"))

    assert out["method"] == "llm"
    assert out["necessary_chunk_ids"] == []
    assert out["multihop_chunk_ids"] == []


@pytest.mark.asyncio
async def test_llm_unavailable_is_fallback_superset() -> None:
    # 호출 실패 → 슬롯 단위 degrade: 청크 전량 necessary 보존, 멀티홉 비움, 실패 사유 rationale.
    llm = _ScriptedLLM(fail=True)
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2", "c3"))

    assert out["method"] == "fallback"
    assert set(out["necessary_chunk_ids"]) == {"c1", "c2", "c3"}
    assert out["multihop_chunk_ids"] == []
    # UI thinking 노출용 실패 사유(러너가 marker 와 함께 노출).
    assert "전량 보존" in out["rationale"]
    assert "LLMUnavailableError" in out["rationale"]


@pytest.mark.asyncio
async def test_malformed_json_is_fallback() -> None:
    # 비 JSON 응답 → 파싱 실패 → 동일 fallback(전량 보존 + 실패 사유).
    llm = _ScriptedLLM(raw="not a json object at all")
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, _chunks("c1", "c2"))

    assert out["method"] == "fallback"
    assert set(out["necessary_chunk_ids"]) == {"c1", "c2"}
    assert out["multihop_chunk_ids"] == []
    assert "전량 보존" in out["rationale"]


@pytest.mark.asyncio
async def test_concurrency_cap_is_respected() -> None:
    # 슬롯 5개를 한 verifier(캡 2)로 동시 검증 → 동시 in-flight 가 2 를 넘지 않아야 한다
    # (슬롯당 1회 호출이므로 동시 호출 = 동시 슬롯 수, 전역 semaphore 가 묶는다).
    llm = _ScriptedLLM({
        "rationale": "keep", "necessary_chunk_ids": ["c0"], "multihop_chunk_ids": [],
    })
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource(), max_concurrency=2)
    results = await asyncio.gather(
        *(verifier.verify_slot(query_text="q", answer_spec="spec",
                               slot_name=f"s{i}", slot_query="sq",
                               chunks=_chunks("c0"))
          for i in range(5))
    )

    assert llm.calls == 5
    assert llm.max_inflight <= 2
    assert all(r["method"] == "llm" for r in results)


@pytest.mark.asyncio
async def test_empty_chunks_is_fallback_noop() -> None:
    llm = _ScriptedLLM()
    verifier = SlotVerifierLlm(llm=llm, source=_FakeSource())
    out = await _verify(verifier, [])

    assert llm.calls == 0
    assert out == {"necessary_chunk_ids": [], "multihop_chunk_ids": [],
                   "rationale": "", "method": "fallback"}
