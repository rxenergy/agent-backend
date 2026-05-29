from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.domain.retrieval import RetrievalPlan, RetrievalStrategy

# v3.1 Node 4 — retrieval_plan_template. 룰 기반 결정론 plan 선택 (LLM 미사용).


def _entity_hash(entities: dict[str, list[str]]) -> str:
    canon = repr(sorted((k, tuple(sorted(v))) for k, v in (entities or {}).items()))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


class RetrievalPlanner:
    """`retrieval_strategies.yaml` 의 `when` 룰을 순차 매칭해 RetrievalPlan 을
    만든다. 첫 매칭 룰의 strategies[] 채택, 없으면 default_strategies."""

    def __init__(
        self,
        *,
        default_strategies: list[str],
        rules: list[dict[str, Any]],
        fusion: str = "rrf",
        rrf_k: int = 60,
    ) -> None:
        self._default = default_strategies or ["hybrid"]
        self._rules = rules or []
        self._fusion = fusion
        self.rrf_k = rrf_k

    @classmethod
    def default(cls) -> "RetrievalPlanner":
        """룰 없는 단일 hybrid planner (테스트/폴백)."""
        return cls(default_strategies=["hybrid"], rules=[], fusion="rrf", rrf_k=60)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RetrievalPlanner":
        body = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            default_strategies=list(body.get("default_strategies") or ["hybrid"]),
            rules=list(body.get("rules") or []),
            fusion=str(body.get("fusion") or "rrf"),
            rrf_k=int(body.get("rrf_k", 60)),
        )

    def plan(
        self,
        *,
        scenario_object: str | None,
        scenario_depth: str | None,
        entities: dict[str, list[str]] | None = None,
        intents: tuple[str, ...] | list[str] = (),
    ) -> RetrievalPlan:
        entities = entities or {}
        intents_l = [str(i).lower() for i in (intents or [])]
        rule_id = "default"
        strategies = list(self._default)
        for rule in self._rules:
            if self._matches(rule.get("when") or {}, scenario_object, entities, intents_l):
                rule_id = str(rule.get("id") or "unnamed")
                strategies = list(rule.get("strategies") or self._default)
                break
        plan_hash = hashlib.sha256(
            f"{rule_id}|{_entity_hash(entities)}".encode("utf-8")
        ).hexdigest()[:16]
        return RetrievalPlan(
            rule_id=rule_id,
            strategies=tuple(RetrievalStrategy(name=s) for s in strategies),
            fusion=self._fusion,
            plan_hash=plan_hash,
        )

    @staticmethod
    def _matches(
        when: dict[str, Any],
        scenario_object: str | None,
        entities: dict[str, list[str]],
        intents_l: list[str],
    ) -> bool:
        so = (scenario_object or "").lower()
        if "scenario_object_in" in when:
            allowed = [str(x).lower() for x in when["scenario_object_in"]]
            if so not in allowed:
                return False
        if "intents_any" in when:
            wanted = [str(x).lower() for x in when["intents_any"]]
            if not any(i in wanted for i in intents_l):
                return False
        if "has_entity_kinds" in when:
            for kind in when["has_entity_kinds"]:
                if not entities.get(kind):
                    return False
        # 빈 when {} 는 무조건 매칭(catch-all 룰 용). 위 조건이 모두 통과면 True.
        return True
