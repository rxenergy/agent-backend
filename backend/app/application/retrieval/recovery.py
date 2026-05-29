from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.domain.retrieval import EvaluationResult

# v3.1 Node 7 — retrieval_recover (docs/plans/hierarchical_corrective_workflow.v1.md §5).
# WEAK/FAIL 진단 → 결정론 복구 액션 → Node 5 재-dispatch → Node 6 재평가. max 2 round.
# LLM 미사용(HyDE/step-back 옵션은 budget 소비 변종 — 후속). 진단→액션은 순수 함수.


@dataclass(frozen=True)
class RecoveryAction:
    strategy_id: str
    entities: dict[str, list[str]]
    fetch_k: int
    min_score: float


class RetrievalRecoverer:
    def __init__(self, synonyms: dict[str, list[str]], *, max_rounds: int = 2,
                 entity_coverage_min: float = 0.3) -> None:
        # 소문자 키 동의어 사전.
        self._syn = {k.lower(): list(v) for k, v in (synonyms or {}).items()}
        self.max_rounds = max_rounds
        self._cov_min = entity_coverage_min

    @classmethod
    def default(cls) -> "RetrievalRecoverer":
        return cls({}, max_rounds=2)

    @classmethod
    def from_yaml_dir(cls, path: str | Path, *, max_rounds: int = 2) -> "RetrievalRecoverer":
        """data/synonyms/*.yaml (term: [syn,...]) 병합. 디렉토리 없으면 빈 사전."""
        merged: dict[str, list[str]] = {}
        p = Path(path)
        if p.is_dir():
            for f in sorted(p.glob("*.yaml")):
                body = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                for k, v in body.items():
                    merged.setdefault(k, [])
                    merged[k].extend(v or [])
        return cls(merged, max_rounds=max_rounds)

    # --- 진단 (순수) ---
    def diagnose(self, evaluation: EvaluationResult) -> str:
        chunks = evaluation.per_chunk
        if not chunks:
            return "no_results"
        avg_cov = sum(c.entity_coverage for c in chunks) / len(chunks)
        avg_score = sum(c.s_total for c in chunks) / len(chunks)
        if avg_cov < self._cov_min:
            return "entity_coverage_low"
        if avg_score < 0.5:
            return "low_scores"
        return "generic"

    # --- 진단 → 액션 (순수) ---
    def plan_action(
        self,
        diagnosis: str,
        *,
        entities: dict[str, list[str]],
        fetch_k: int,
        min_score: float,
    ) -> RecoveryAction:
        if diagnosis == "entity_coverage_low":
            expanded = self._expand_synonyms(entities)
            if expanded != entities:
                return RecoveryAction("synonym_expand", expanded, fetch_k, min_score)
            # 동의어가 없으면 filter 완화로 폴백(무한 동일검색 방지).
            return RecoveryAction("relax_filter", entities, fetch_k * 2, 0.0)
        # low_scores / generic / no_results → 풀 확대 + 필터 완화.
        return RecoveryAction("relax_filter", entities, fetch_k * 2, 0.0)

    def _expand_synonyms(self, entities: dict[str, list[str]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for kind, vals in (entities or {}).items():
            seen = list(vals)
            for v in vals:
                for syn in self._syn.get(v.lower(), []):
                    if syn not in seen:
                        seen.append(syn)
            out[kind] = seen
        return out
