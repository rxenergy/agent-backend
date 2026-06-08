from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Terminology Canonicalization & Expansion 자산 로더
# (docs/plans/terminology_normalization_strategy.v1.md).
#
# ISO 25964 통제어휘 — concept 단위(preferred term + UF/BT/NT/RT). 두 *결정론* 연산:
#   canonicalize(정밀): surface/uf → preferred + definition. N1.5 conductor-invoked
#                       (보장 실행). 용어집 lookup 으로 정밀도를 올린다.
#   expand(재현):       term → 관계어(uf/nt 기본, rt opt-in). Finder recover-gated.
#                       시소러스로 재검색 범위를 넓힌다(선택적 확장 — query drift 통제).
# 둘 다 LLM 미사용(순수 함수). 표현=모델 / 결정=데이터 분리([[feedback_model_over_rule]]):
# 번역·개념 식별은 모델(N0/분류)이, 정규형·관계어 lookup 은 이 데이터가 담당한다.
#
# 재현성: vocab_sha = sha256(canonical json)[:16] — CorpusMap.corpus_map_hash 동형.
# "어떤 어휘 자산으로 이 검색이 돌았나"를 event 가 단독 설명한다(원칙 5). prompt 처럼
# *선언 sha 핀*이 아니라 *내용 해시*다(데이터 자산은 CorpusMap 과 동일 모델).
#
# 미등록 term 은 passthrough(원형 보존, silent drop 금지 — 현 retrieval.normalize 규약 계승).

_RELATIONS = ("uf", "bt", "nt", "rt")


def _hash(body: dict[str, Any]) -> str:
    canon = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _tuple(v: Any) -> tuple[str, ...]:
    if not v:
        return ()
    if isinstance(v, (list, tuple, set)):
        return tuple(str(x).strip() for x in v if str(x).strip())
    s = str(v).strip()
    return (s,) if s else ()


@dataclass(frozen=True)
class Concept:
    """ISO 25964 concept — preferred term + lead-in entries(UF) + 패러다임 관계."""

    concept_id: str
    preferred: str
    definition_en: str = ""
    definition_ko: str = ""
    uf: tuple[str, ...] = ()  # USE FOR — 동의어·약어(canonicalize 입력 + expand)
    bt: tuple[str, ...] = ()  # Broader Term
    nt: tuple[str, ...] = ()  # Narrower Term(expand 기본 포함)
    rt: tuple[str, ...] = ()  # Related Term(affinitive — expand opt-in, drift 위험)

    def definition(self) -> str:
        # 내부 워크플로우는 영어 — en 우선, ko 병기(현 _TERM_DICT 의 ko(en) 와 반대로
        # en(ko); 검색·추론이 영어라 영어 정의가 1차).
        if self.definition_en and self.definition_ko:
            return f"{self.definition_en} ({self.definition_ko})"
        return self.definition_en or self.definition_ko

    def related(self, relations: Iterable[str]) -> list[str]:
        out: list[str] = []
        for rel in relations:
            out.extend(getattr(self, rel, ()) or ())
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.concept_id, "preferred": self.preferred,
            "definition_en": self.definition_en, "definition_ko": self.definition_ko,
            "uf": list(self.uf), "bt": list(self.bt),
            "nt": list(self.nt), "rt": list(self.rt),
        }


@dataclass(frozen=True)
class CanonicalizeResult:
    """N1.5 산출. canonical_terms 는 입력 순서(미등록은 원형 passthrough)."""

    canonical_terms: tuple[str, ...]
    definitions: dict[str, str]        # {preferred: definition}
    concept_ids: tuple[str, ...]       # 매칭 concept id(중복 제거, 등장 순서) — 재현 핀
    unresolved: tuple[str, ...]        # 미등록 term(원형 보존)


@dataclass(frozen=True)
class ExpandResult:
    """Finder recover 산출. expanded_terms 는 중복 제거(원 term/preferred 제외)."""

    expanded_terms: tuple[str, ...]
    relations: dict[str, list[str]]    # {입력 term: [관계어...]}


class TerminologyVocabError(RuntimeError):
    """vocab.yaml 적재/검증 실패(파일 없음 · 필드 누락 · surface 충돌)."""


class TerminologyVocab:
    """`tools/terminology/vocab.yaml`(ISO 25964) 결정론 lookup.

    surface(소문자) → concept 색인을 preferred + UF 변이형으로 만든다. 한 surface 가
    서로 다른 concept 로 매핑되면 어휘 모순이므로 boot 시 fail-fast(무결성)."""

    def __init__(
        self,
        *,
        concepts: list[Concept],
        version: str = "v1",
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.version = version
        self._concepts = list(concepts)
        self._by_id = {c.concept_id: c for c in self._concepts}
        self._index: dict[str, Concept] = {}
        for c in self._concepts:
            for surface in (c.preferred, *c.uf):
                key = surface.strip().lower()
                if not key:
                    continue
                existing = self._index.get(key)
                if existing is not None and existing.concept_id != c.concept_id:
                    raise TerminologyVocabError(
                        f"surface {surface!r} maps to both {existing.concept_id!r} "
                        f"and {c.concept_id!r} (어휘 모순)"
                    )
                self._index[key] = c
        # 해시는 *원본 본문* 기준(로더가 본 그대로). 직접 생성(테스트)이면 concept 직렬화.
        self.vocab_sha = _hash(
            raw if raw is not None
            else {"version": version, "concepts": [c.as_dict() for c in self._concepts]}
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "TerminologyVocab":
        import yaml

        p = Path(path)
        if not p.is_file():
            raise TerminologyVocabError(f"terminology vocab not found at {p}")
        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        version = str(body.get("version") or "v1")
        concepts: list[Concept] = []
        for raw_c in body.get("concepts") or []:
            cid = str(raw_c.get("id") or "").strip()
            preferred = str(raw_c.get("preferred") or cid).strip()
            if not cid or not preferred:
                raise TerminologyVocabError(
                    f"concept requires id + preferred: {raw_c!r}"
                )
            concepts.append(
                Concept(
                    concept_id=cid, preferred=preferred,
                    definition_en=str(raw_c.get("definition_en") or "").strip(),
                    definition_ko=str(raw_c.get("definition_ko") or "").strip(),
                    uf=_tuple(raw_c.get("uf")), bt=_tuple(raw_c.get("bt")),
                    nt=_tuple(raw_c.get("nt")), rt=_tuple(raw_c.get("rt")),
                )
            )
        return cls(concepts=concepts, version=version, raw=body)

    @classmethod
    def default(cls) -> "TerminologyVocab":
        """빈 어휘(자산 미배치 폴백) — canonicalize=passthrough, expand=빈 결과."""
        return cls(concepts=[], version="v1", raw={"version": "v1", "concepts": []})

    # ------------------------------------------------------------------
    def canonicalize(self, terms: Iterable[str]) -> CanonicalizeResult:
        """surface/uf → preferred + definition(정밀). 미등록은 원형 passthrough."""
        canonical: list[str] = []
        definitions: dict[str, str] = {}
        concept_ids: list[str] = []
        unresolved: list[str] = []
        seen_ids: set[str] = set()
        for raw in terms:
            term = str(raw or "").strip()
            if not term:
                continue
            c = self._index.get(term.lower())
            if c is None:
                canonical.append(term)
                unresolved.append(term)
                continue
            canonical.append(c.preferred)
            d = c.definition()
            if d:
                definitions[c.preferred] = d
            if c.concept_id not in seen_ids:
                seen_ids.add(c.concept_id)
                concept_ids.append(c.concept_id)
        return CanonicalizeResult(
            canonical_terms=tuple(canonical), definitions=definitions,
            concept_ids=tuple(concept_ids), unresolved=tuple(unresolved),
        )

    def expand(
        self,
        terms: Iterable[str],
        *,
        relations: Iterable[str] = ("uf", "nt"),
        max_per_term: int | None = None,
    ) -> ExpandResult:
        """term → 관계어(재현). 기본 uf+nt(rt 는 affinitive 라 opt-in). 원 term/preferred
        과 이미 산출된 관계어는 제외(중복·자기참조 방지). max_per_term 으로 term 당 상한."""
        rels = tuple(r for r in relations if r in _RELATIONS)
        expanded: list[str] = []
        rel_map: dict[str, list[str]] = {}
        seen: set[str] = set()
        for raw in terms:
            term = str(raw or "").strip()
            if not term:
                continue
            c = self._index.get(term.lower())
            if c is None:
                continue
            base = {term.lower(), c.preferred.lower()}
            picked: list[str] = []
            for r in c.related(rels):
                rl = r.strip()
                low = rl.lower()
                if not rl or low in base or low in seen:
                    continue
                picked.append(rl)
                seen.add(low)
                if max_per_term and len(picked) >= max_per_term:
                    break
            if picked:
                rel_map[term] = picked
                expanded.extend(picked)
        return ExpandResult(expanded_terms=tuple(expanded), relations=rel_map)
