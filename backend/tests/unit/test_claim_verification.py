from __future__ import annotations

import json

import pytest

from app.application.verification.claim_decompose import ClaimDecomposer
from app.application.verification.claim_verifier import ClaimVerifier
from app.application.verification.entailment import EntailmentChecker, EntailmentVerdict
from app.domain.verification import Claim, ClaimStatus


class _JSONLLM:
    """grammar 무시, 고정 텍스트 반환 fake."""

    model_id = "json-fake"

    def __init__(self, text: str) -> None:
        self._text = text

    async def generate(self, prompt, *, model_options=None, grammar=None):
        from app.ports.llm import LLMResult
        return LLMResult(text=self._text, token_usage={}, model_id=self.model_id)


class _BoomLLM:
    model_id = "boom"

    async def generate(self, prompt, *, model_options=None, grammar=None):
        raise RuntimeError("upstream down")


# --- decompose --------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_llm_path_parses_json():
    llm = _JSONLLM(json.dumps({"claims": [
        {"id": "cl-0", "text": "A [cite-1]", "cite_marker": "cite-1"},
        {"id": "cl-1", "text": "B"},
    ]}))
    res = await ClaimDecomposer(llm).decompose("answer")
    assert res.method == "llm"
    assert [c.id for c in res.claims] == ["cl-0", "cl-1"]
    assert res.claims[0].cite_marker == "cite-1"


@pytest.mark.asyncio
async def test_decompose_falls_back_on_unavailable_and_records_method():
    res = await ClaimDecomposer(_BoomLLM()).decompose(
        "i-SMR uses passive ECCS [cite-0]. Second claim without cite."
    )
    assert res.method == "fallback"
    assert len(res.claims) == 2
    assert res.claims[0].cite_marker == "cite-0"
    assert res.claims[1].cite_marker is None  # 미인용 문장도 claim 으로(은폐 X)


@pytest.mark.asyncio
async def test_decompose_falls_back_on_garbage_json():
    res = await ClaimDecomposer(_JSONLLM("not json at all")).decompose("X [cite-0].")
    assert res.method == "fallback"


# --- verifier 4-step branches ----------------------------------------------


class _FakeEntailment:
    """controllable entailment — claim_id → status. 빈 dict 면 '미실행'."""

    def __init__(self, verdicts: dict[str, str] | None) -> None:
        self._v = verdicts

    @property
    def model_id(self) -> str:
        return "fake-entail"

    async def check(self, claims, *, evidence_by_cite):
        if self._v is None:
            return {}
        return {cid: EntailmentVerdict(status=st, score=0.9) for cid, st in self._v.items()}


def _claims():
    return [Claim(id="cl-0", text="i-SMR ECCS passive", cite_marker="cite-0")]


@pytest.mark.asyncio
async def test_supported_when_citation_resolves_and_entailment_supported():
    v = ClaimVerifier(_FakeEntailment({"cl-0": "supported"}))
    res = await v.verify(
        _claims(), resolvable_citation_ids={"cite-0"}, candidate_citation_ids={"cite-0"},
        evidence_by_cite={"cite-0": "i-SMR ECCS passive cooling"},
    )
    assert res.claims[0].status == ClaimStatus.SUPPORTED.value
    assert res.status == "pass"
    assert res.entailment_ran is True


@pytest.mark.asyncio
async def test_unsupported_when_entailment_low():
    v = ClaimVerifier(_FakeEntailment({"cl-0": "unsupported"}))
    res = await v.verify(
        _claims(), resolvable_citation_ids={"cite-0"}, candidate_citation_ids={"cite-0"},
        evidence_by_cite={"cite-0": "unrelated"},
    )
    assert res.claims[0].status == ClaimStatus.UNSUPPORTED.value
    assert res.status == "partial"


@pytest.mark.asyncio
async def test_unsupported_when_citation_does_not_resolve_regardless_of_entailment():
    v = ClaimVerifier(_FakeEntailment({"cl-0": "supported"}))
    res = await v.verify(
        _claims(), resolvable_citation_ids=set(), candidate_citation_ids=set(),
        evidence_by_cite={},
    )
    assert res.claims[0].status == ClaimStatus.UNSUPPORTED.value


@pytest.mark.asyncio
async def test_contradicted_sets_fail_aggregate():
    v = ClaimVerifier(_FakeEntailment({"cl-0": "contradicted"}))
    res = await v.verify(
        _claims(), resolvable_citation_ids={"cite-0"}, candidate_citation_ids={"cite-0"},
        evidence_by_cite={"cite-0": "i-SMR does NOT use passive ECCS"},
    )
    assert res.claims[0].status == ClaimStatus.CONTRADICTED.value
    assert res.contradicted is True
    assert res.status == "fail"


@pytest.mark.asyncio
async def test_entailment_unavailable_degrades_to_citation_grounded():
    """entailment 미실행(None) → citation 만으로 supported(degrade), entailment_ran=False."""
    v = ClaimVerifier(_FakeEntailment(None))
    res = await v.verify(
        _claims(), resolvable_citation_ids={"cite-0"}, candidate_citation_ids={"cite-0"},
        evidence_by_cite={"cite-0": "x"},
    )
    assert res.claims[0].status == ClaimStatus.SUPPORTED.value
    assert res.entailment_ran is False


@pytest.mark.asyncio
async def test_version_conflict_forces_contradicted():
    v = ClaimVerifier(_FakeEntailment({"cl-0": "supported"}))
    res = await v.verify(
        _claims(), resolvable_citation_ids={"cite-0"}, candidate_citation_ids={"cite-0"},
        evidence_by_cite={"cite-0": "x"},
        version_constraint="2024-06-01", revision_by_cite={"cite-0": "2019-01-01"},
    )
    # revision(2019) < constraint(2024) → version_match False → contradicted.
    assert res.claims[0].status == ClaimStatus.CONTRADICTED.value
    assert res.status == "fail"


@pytest.mark.asyncio
async def test_empty_claims_is_partial():
    res = await ClaimVerifier(_FakeEntailment({})).verify(
        [], resolvable_citation_ids=set(), candidate_citation_ids=set(), evidence_by_cite={},
    )
    assert res.status == "partial"


@pytest.mark.asyncio
async def test_entailment_checker_parses_verdicts():
    llm = _JSONLLM(json.dumps({"verdicts": [{"claim_id": "cl-0", "status": "supported", "score": 0.9}]}))
    out = await EntailmentChecker(llm).check([Claim(id="cl-0", text="x")], evidence_by_cite={})
    assert out["cl-0"].status == "supported"


@pytest.mark.asyncio
async def test_entailment_checker_returns_empty_on_failure():
    out = await EntailmentChecker(_BoomLLM()).check([Claim(id="cl-0", text="x")], evidence_by_cite={})
    assert out == {}
