from __future__ import annotations

from pathlib import Path

from app.application.retrieval.evaluator import RetrievalEvaluator
from app.domain.retrieval import GateDecision, RetrievedChunk

_POLICY_YAML = Path(__file__).resolve().parents[3] / "tools" / "evaluator_policy.yaml"


def _ev() -> RetrievalEvaluator:
    return RetrievalEvaluator.from_yaml(_POLICY_YAML)


def _chunk(cid="c1", *, text, score=0.8, authority_tier=None, clause_id=None,
           jurisdiction=None, effective_on=None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, document_id="d", score=score, snippet=text,
        authority_tier=authority_tier, clause_id=clause_id,
        jurisdiction=jurisdiction, effective_on=effective_on,
    )


# --- positive: clean PASS ---------------------------------------------------


def test_clean_positive_passes():
    ev = _ev()
    c = _chunk(text="i-SMR ECCS passive cooling design RG 1.157",
               authority_tier="secondary", clause_id="RG_1_157", jurisdiction="NRC")
    res = ev.evaluate(
        [c], query_text="i-SMR ECCS RG 1.157",
        entities={"reactor_type": ["i-SMR"]}, rerank_scores={"c1": 0.016},
        regulatory_enforced=True,
    )
    assert res.per_chunk[0].decision == GateDecision.PASS.value
    assert res.overall_decision == GateDecision.PASS.value
    assert res.per_chunk[0].hard_gates_passed is True


# --- negative: version conflict → FAIL (always, even on v1) -----------------


def test_version_conflict_fails():
    ev = _ev()
    # effective_on(2019) < version_constraint(2024-06-01) → 충돌 확정 → 강한 음성.
    c = _chunk(text="ECCS i-SMR design fully matching the query terms",
               authority_tier="primary", effective_on="2019-01-01")
    res = ev.evaluate(
        [c], query_text="ECCS i-SMR design", entities={},
        version_constraint="2024-06-01", rerank_scores={"c1": 0.016},
        regulatory_enforced=True,
    )
    assert res.per_chunk[0].hard_gates_passed is False
    assert res.per_chunk[0].decision == GateDecision.FAIL.value
    assert res.overall_decision == GateDecision.FAIL.value


# --- negative: tertiary under enforcement → FAIL ----------------------------


def test_tertiary_under_enforcement_fails():
    ev = _ev()
    c = _chunk(text="NuScale vendor ECCS submission matching query terms",
               authority_tier="tertiary")
    res = ev.evaluate(
        [c], query_text="NuScale vendor ECCS submission", entities={},
        rerank_scores={"c1": 0.016}, regulatory_enforced=True,
    )
    assert res.per_chunk[0].hard_gates_passed is False
    assert res.per_chunk[0].decision == GateDecision.FAIL.value


def test_tertiary_allowed_when_not_enforced():
    """v1 path — same tertiary chunk passes the hard gate when regulatory
    enforcement is off (vendor docs not rejected)."""
    ev = _ev()
    c = _chunk(text="NuScale vendor ECCS submission matching query terms",
               authority_tier="tertiary")
    res = ev.evaluate(
        [c], query_text="NuScale vendor ECCS submission", entities={},
        rerank_scores={"c1": 0.016}, regulatory_enforced=False,
    )
    assert res.per_chunk[0].hard_gates_passed is True
    assert res.regulatory_enforced is False


# --- negative: low entity coverage → hard fail ------------------------------


def test_low_entity_coverage_fails():
    ev = _ev()
    # entity 'NuScale' 가 본문에 없음 → coverage 0 < 0.3 → hard fail.
    c = _chunk(text="i-SMR ECCS passive design", authority_tier="secondary")
    res = ev.evaluate(
        [c], query_text="i-SMR ECCS", entities={"reactor_type": ["NuScale"]},
        rerank_scores={"c1": 0.016}, regulatory_enforced=False,
    )
    assert res.per_chunk[0].entity_coverage == 0.0
    assert res.per_chunk[0].hard_gates_passed is False
    assert res.per_chunk[0].decision == GateDecision.FAIL.value


# --- v1 NA path: unknown regulatory → PASS-eligible, flag false --------------


def test_v1_unknown_regulatory_is_pass_eligible_but_flagged():
    """결정 #3: v1 에서 regulatory 입력 부재여도 lexical 이 강하면 PASS 가능.
    단 regulatory_enforced=false 로 '검증된 PASS 아님' 표면화."""
    ev = _ev()
    # authority_tier 만 collection 유도(secondary), clause/version/jurisdiction None.
    c = _chunk(text="i-SMR ECCS passive cooling design overview",
               authority_tier="secondary")
    res = ev.evaluate(
        [c], query_text="i-SMR ECCS passive cooling design",
        entities={"reactor_type": ["i-SMR"]}, rerank_scores={"c1": 0.016},
        regulatory_enforced=False,
    )
    assert res.per_chunk[0].decision == GateDecision.PASS.value
    assert res.regulatory_enforced is False  # PASS 이지만 규제 미검증 표면화


# --- aggregation + policy hash ----------------------------------------------


def test_per_sq_weak_when_only_weak_chunks():
    ev = _ev()
    # 결정론적 WEAK 고정: query 5토큰{i,smr,eccs,passive,design} 중 본문은 2개만
    # 매칭 → lexical 0.4. tier 없음 → s_reg 0. 단일 chunk → s_sem(rerank) 1.0.
    # S_total = 0.40·0.4 + 0.40·0 + 0.20·1.0 = 0.36 ∈ [τ_weak .3, τ_pass .5) → WEAK.
    c = _chunk(text="passive design", authority_tier=None)
    res = ev.evaluate(
        [c], query_text="i-SMR ECCS passive design", entities={},
        rerank_scores={"c1": 0.016}, regulatory_enforced=False,
    )
    assert res.per_chunk[0].s_lex == 0.4
    assert res.per_chunk[0].decision == GateDecision.WEAK.value
    # PASS 0 · WEAK 1 → sq 는 WEAK(복구 대상).
    assert res.overall_decision == GateDecision.WEAK.value


def test_policy_hash_deterministic_and_present():
    a = _ev()
    b = _ev()
    assert a.policy_hash == b.policy_hash
    res = a.evaluate([_chunk(text="x")], query_text="x", entities={}, rerank_scores={})
    assert res.evaluator_policy_hash == a.policy_hash


def test_empty_chunks_overall_fail():
    res = _ev().evaluate([], query_text="q", entities={}, rerank_scores={})
    assert res.overall_decision == GateDecision.FAIL.value
