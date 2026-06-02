from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# v3.1 Layer 1 — 범위 한정(scope narrowing). 문서 구조 사전지식(corpus_map)을
# 룰로 읽어 (scenario_object / entities / intents)를 검색 scope 로 해석한다.
# LLM 미사용(결정론) — self-querying retriever 를 코퍼스 맵으로 구현한 형태.
#
# confidence-게이트(핵심): 잘못된 hard filter 는 정답을 도달 불가로 만든다
# (recall 절벽). 따라서 분류 confidence 가 높을 때만 filter(모집단 제한), 중간
# 이면 boost(가산만, 전 코퍼스 유지), 낮으면 off. 노이즈 floor(min_token_count)
# 는 scope 와 직교한 품질 신호라 mode 와 무관하게 항상 적용한다.
#
# 재현성: corpus_map_hash = sha256(canonical json)[:16] — evaluator._policy_hash
# 와 동일 idiom. 어느 맵이 이 scope 를 만들었나를 event 가 단독으로 설명.


def _hash(body: dict[str, Any]) -> str:
    canon = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ScopeDecision:
    """resolve_scope 산출물. `mode` 가 target/filters 중 무엇이 채워졌는지 결정.

    - mode="filter": filters 채움(hard-scope, high confidence). target 비움.
    - mode="boost" : target 채움(boost-scope, mid confidence). filters 비움.
    - mode="off"   : 둘 다 비움(low/ambiguous confidence) — 전 코퍼스.
    min_token_count 는 노이즈 floor 로 mode 와 무관하게 실린다."""

    mode: str = "off"
    target: dict[str, list[str]] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    min_token_count: int = 0
    matched_rule_id: str | None = None
    corpus_map_hash: str | None = None


class CorpusMap:
    """`corpus_map.yaml` 의 `topic_routing` 룰을 순차 매칭해 scope 를 만든다.

    룰 `when`(모두 AND):
      scenario_object_in: [..]   — scenario_object 가 목록에 포함
      has_entity_kinds:   [..]   — entities 에 해당 종류가 모두 존재
      intents_any:        [..]   — intents 중 하나라도 목록에 포함
                                    (Node 3 stub 동안 intents 는 비어 있어 inert —
                                     forward-compatible 룰용)
    첫 매칭 룰의 `scope`(collection/search_type 등) 를 채택, 없으면 scope 미적용
    (mode 는 off, 단 noise floor 는 적용)."""

    def __init__(
        self,
        *,
        collections: dict[str, Any] | None = None,
        topic_routing: list[dict[str, Any]] | None = None,
        chunk_quality: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.collections = collections or {}
        self._rules = topic_routing or []
        self._chunk_quality = chunk_quality or {}
        self.min_token_count = int(self._chunk_quality.get("min_token_count", 0) or 0)
        # 해시는 *원본 본문* 기준(로더가 본 그대로). default()/from_yaml 모두 동일.
        self.corpus_map_hash = _hash(
            raw
            if raw is not None
            else {
                "collections": self.collections,
                "topic_routing": self._rules,
                "chunk_quality": self._chunk_quality,
            }
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CorpusMap":
        import yaml

        body = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            collections=dict(body.get("collections") or {}),
            topic_routing=list(body.get("topic_routing") or []),
            chunk_quality=dict(body.get("chunk_quality") or {}),
            raw=body,
        )

    @classmethod
    def default(cls) -> "CorpusMap":
        """룰 없는 빈 맵(scope off) — noise floor 도 0. 맵 미배치 시 폴백."""
        return cls(collections={}, topic_routing=[], chunk_quality={})

    # ------------------------------------------------------------------
    def resolve_scope(
        self,
        *,
        scenario_object: str | None,
        scenario_depth: str | None = None,
        intents: tuple[str, ...] | list[str] = (),
        entities: dict[str, list[str]] | None = None,
        confidence: float,
        tau_high: float,
        tau_low: float,
        settings_min_token_count: int = 0,
    ) -> ScopeDecision:
        """confidence-게이트로 ScopeDecision 을 만든다.

        min_token_count 우선순위(plan 결정 #3): corpus_map.chunk_quality 값 >
        settings 기본(맵이 값을 안 주면). scope mode 와 무관하게 실린다."""
        entities = entities or {}
        floor = self.min_token_count if self.min_token_count > 0 else int(settings_min_token_count or 0)

        # confidence 가 boost 임계 미만 → scope off(전 코퍼스), floor 만.
        if confidence < tau_low:
            return ScopeDecision(
                mode="off", min_token_count=floor, corpus_map_hash=self.corpus_map_hash,
            )

        rule = self._match(scenario_object, entities, intents)
        if rule is None:
            # 매칭 룰 없음 → 좁힐 근거 없음. off + floor.
            return ScopeDecision(
                mode="off", min_token_count=floor, corpus_map_hash=self.corpus_map_hash,
            )

        scope = {k: v for k, v in (rule.get("scope") or {}).items() if v}
        rule_id = str(rule.get("id") or "unnamed")
        if not scope:
            return ScopeDecision(
                mode="off", min_token_count=floor, matched_rule_id=rule_id,
                corpus_map_hash=self.corpus_map_hash,
            )

        # high confidence → hard filter(모집단 제한). mid → boost(가산만).
        if confidence >= tau_high:
            return ScopeDecision(
                mode="filter", filters=self._as_filters(scope), min_token_count=floor,
                matched_rule_id=rule_id, corpus_map_hash=self.corpus_map_hash,
            )
        return ScopeDecision(
            mode="boost", target=self._as_target(scope), min_token_count=floor,
            matched_rule_id=rule_id, corpus_map_hash=self.corpus_map_hash,
        )

    # ------------------------------------------------------------------
    def _match(
        self,
        scenario_object: str | None,
        entities: dict[str, list[str]],
        intents: tuple[str, ...] | list[str],
    ) -> dict[str, Any] | None:
        so = (scenario_object or "").lower()
        intents_l = [str(i).lower() for i in (intents or [])]
        for rule in self._rules:
            when = rule.get("when") or {}
            if "scenario_object_in" in when:
                allowed = [str(x).lower() for x in when["scenario_object_in"]]
                if so not in allowed:
                    continue
            if "has_entity_kinds" in when:
                if not all(entities.get(k) for k in when["has_entity_kinds"]):
                    continue
            if "intents_any" in when:
                wanted = [str(x).lower() for x in when["intents_any"]]
                if not any(i in wanted for i in intents_l):
                    continue
            return rule
        return None

    @staticmethod
    def _as_filters(scope: dict[str, Any]) -> dict[str, Any]:
        # scope 값은 collection/search_type 등 인덱스 keyword 필드 → 그대로 term/terms.
        return dict(scope)

    @staticmethod
    def _as_target(scope: dict[str, Any]) -> dict[str, list[str]]:
        # boost-scope 는 리스트 형태로 정규화(build_hybrid_query 의 terms boost 입력).
        out: dict[str, list[str]] = {}
        for k, v in scope.items():
            if isinstance(v, (list, tuple, set)):
                out[k] = [str(x) for x in v if x not in (None, "")]
            elif v not in (None, ""):
                out[k] = [str(v)]
        return out
