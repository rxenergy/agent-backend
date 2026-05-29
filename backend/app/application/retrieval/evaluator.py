from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.application.retrieval import signals
from app.domain.retrieval import (
    ChunkSignals,
    EvaluationResult,
    GateDecision,
    RetrievedChunk,
    SubQuestionDecision,
)

# v3.1 Node 6 — retrieval_evaluate. 검색 결과가 답할 자격이 있는지 결정론적으로
# 게이팅한다(spec §6: "답변보다 더 엄격히 설계되어야 한다").
#
# 결합 = Hard gate(tri-state) + Linear Weighted Sum.
#   hard gate: *명확한 negative* 만 FAIL. 입력 부재(None) → not-applicable(차단X).
#   S_total  : Σ w_i·s_i / Σ w_i (활성 가중치 정규화).
#   per_chunk: PASS(>=τ_pass) / WEAK(>=τ_weak) / FAIL. hard gate 탈락은 즉시 FAIL.
#   per_sq   : n_pass>=k_min → PASS; (n_pass+n_weak)>=k_min & n_pass>=1 → WEAK; else FAIL.
#
# v1/v2 차이: authority_tier hard gate 는 `regulatory_enforced` 가 true 일 때만
# 적용(opensearch_schema_version=="v2"). version 충돌은 확정 시 항상 FAIL(강한
# 음성). `regulatory_enforced` 는 결과에 실려 "v1 PASS ≠ 검증된 PASS"를 표면화.


def _policy_hash(policy: dict[str, Any]) -> str:
    canon = json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


class RetrievalEvaluator:
    def __init__(self, policy: dict[str, Any]) -> None:
        self._p = policy
        self._weights: dict[str, float] = dict(policy.get("weights") or {})
        thr = policy.get("thresholds") or {}
        self._tau_pass = float(thr.get("tau_pass", 0.5))
        self._tau_weak = float(thr.get("tau_weak", 0.3))
        self._k_min = int((policy.get("sub_question") or {}).get("k_min", 1))
        hg = policy.get("hard_gates") or {}
        self._entity_cov_min = float(hg.get("entity_coverage_min", 0.3))
        self._min_tier = str(hg.get("min_authority_tier", "secondary"))
        self._tier_rank: dict[str, int] = {
            str(k).lower(): int(v) for k, v in (policy.get("authority_tier_rank") or {}).items()
        }
        self._tier_score: dict[str, float] = {
            str(k).lower(): float(v) for k, v in (policy.get("authority_tier_score") or {}).items()
        }
        self.policy_hash = _policy_hash(policy)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RetrievalEvaluator":
        import yaml

        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    @classmethod
    def default(cls) -> "RetrievalEvaluator":
        return cls(
            {
                "weights": {"lexical": 0.40, "semantic": 0.0, "regulatory": 0.40, "ensemble": 0.20},
                "thresholds": {"tau_pass": 0.5, "tau_weak": 0.3},
                "sub_question": {"k_min": 1},
                "hard_gates": {"entity_coverage_min": 0.3, "min_authority_tier": "secondary"},
                "authority_tier_rank": {"primary": 3, "secondary": 2, "tertiary": 1},
                "authority_tier_score": {"primary": 1.0, "secondary": 0.6, "tertiary": 0.3},
            }
        )

    def evaluate(
        self,
        chunks: list[RetrievedChunk],
        *,
        query_text: str,
        entities: dict[str, list[str]] | None = None,
        version_constraint: str | None = None,
        rrf_scores: dict[str, float] | None = None,
        regulatory_enforced: bool = False,
        sub_question_id: str = "sq0",
    ) -> EvaluationResult:
        rrf_scores = rrf_scores or {}
        max_rrf = max(rrf_scores.values(), default=0.0)
        per_chunk: list[ChunkSignals] = []
        n_pass = n_weak = n_fail = 0

        for c in chunks:
            s_lex = signals.lexical_coverage(query_text, c)
            s_sem = 0.0  # G2 cross-encoder — PR-5 미구현(P5)
            s_reg = signals.regulatory_signal(
                c, version_constraint=version_constraint, tier_score=self._tier_score
            )
            s_ens = signals.ensemble_signal(c.chunk_id, rrf_scores, max_rrf)
            ent_cov = signals.entity_coverage(entities, c)

            # --- Hard gate (tri-state) ---
            hard_fail = False
            # version: 충돌 확정 시 항상 FAIL.
            if signals.version_conflict(c, version_constraint) is True:
                hard_fail = True
            # authority_tier: enforce 플래그 켜졌고 tier 가 known & 임계 미만이면 FAIL.
            if regulatory_enforced and not hard_fail:
                rank = signals.authority_rank(c, self._tier_rank)
                min_rank = self._tier_rank.get(self._min_tier.lower())
                if rank is not None and min_rank is not None and rank < min_rank:
                    hard_fail = True
            # entity_coverage: entities 존재 시에만 적용.
            if not hard_fail and ent_cov < self._entity_cov_min:
                hard_fail = True

            s_total = self._weighted_sum(
                {"lexical": s_lex, "semantic": s_sem, "regulatory": s_reg, "ensemble": s_ens}
            )
            if hard_fail:
                decision = GateDecision.FAIL.value
            elif s_total >= self._tau_pass:
                decision = GateDecision.PASS.value
            elif s_total >= self._tau_weak:
                decision = GateDecision.WEAK.value
            else:
                decision = GateDecision.FAIL.value

            if decision == GateDecision.PASS.value:
                n_pass += 1
            elif decision == GateDecision.WEAK.value:
                n_weak += 1
            else:
                n_fail += 1

            per_chunk.append(
                ChunkSignals(
                    chunk_id=c.chunk_id,
                    s_lex=round(s_lex, 4),
                    s_sem=round(s_sem, 4),
                    s_reg=round(s_reg, 4),
                    s_ens=round(s_ens, 4),
                    s_total=round(s_total, 4),
                    entity_coverage=round(ent_cov, 4),
                    hard_gates_passed=not hard_fail,
                    decision=decision,
                )
            )

        if n_pass >= self._k_min:
            sq_decision = GateDecision.PASS.value
        elif (n_pass + n_weak) >= self._k_min and n_pass >= 1:
            sq_decision = GateDecision.WEAK.value
        elif (n_pass + n_weak) >= 1:
            # PASS 0개지만 WEAK 가 있으면 WEAK(복구 대상). 전부 FAIL 이면 FAIL.
            sq_decision = GateDecision.WEAK.value
        else:
            sq_decision = GateDecision.FAIL.value

        return EvaluationResult(
            per_chunk=tuple(per_chunk),
            per_sub_question=(
                SubQuestionDecision(
                    sub_question_id=sub_question_id,
                    decision=sq_decision,
                    n_pass=n_pass,
                    n_weak=n_weak,
                    n_fail=n_fail,
                ),
            ),
            overall_decision=sq_decision,
            evaluator_policy_hash=self.policy_hash,
            regulatory_enforced=regulatory_enforced,
        )

    def _weighted_sum(self, scores: dict[str, float]) -> float:
        num = 0.0
        denom = 0.0
        for name, w in self._weights.items():
            if w <= 0:
                continue
            num += w * float(scores.get(name, 0.0))
            denom += w
        return num / denom if denom > 0 else 0.0
