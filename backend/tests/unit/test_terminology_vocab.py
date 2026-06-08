from __future__ import annotations

from pathlib import Path

import pytest

from app.application.terminology.vocab import (
    Concept,
    TerminologyVocab,
    TerminologyVocabError,
)

# P1 — Terminology 통제어휘 로더(ISO 25964). canonicalize(정밀)/expand(재현) 두 결정론
# 연산 + vocab_sha 재현 핀. 설계: docs/plans/terminology_normalization_strategy.v1.md.
# 워크플로우 배선(N1.5 canonicalize / Finder recover expand)은 P2/P3 — 여기선 자산·로더만.

_REPO_ROOT = Path(__file__).resolve().parents[3]
_VOCAB = _REPO_ROOT / "tools" / "terminology" / "vocab.yaml"


def _repo_vocab() -> TerminologyVocab:
    return TerminologyVocab.from_yaml(_VOCAB)


# --- 로드 / 무결성 -----------------------------------------------------------
def test_repo_asset_loads_and_pins_sha() -> None:
    v = _repo_vocab()
    assert v.version == "v1"
    assert v.vocab_sha and len(v.vocab_sha) == 16  # 재현 핀(sha16).


def test_missing_file_fails_fast(tmp_path) -> None:
    with pytest.raises(TerminologyVocabError):
        TerminologyVocab.from_yaml(tmp_path / "nope.yaml")


def test_surface_collision_fails_fast() -> None:
    # 한 surface 가 두 concept 로 매핑되면 어휘 모순 → boot fail-fast.
    with pytest.raises(TerminologyVocabError):
        TerminologyVocab(concepts=[
            Concept(concept_id="A", preferred="A", uf=("shared",)),
            Concept(concept_id="B", preferred="B", uf=("SHARED",)),  # 대소문자 무시 충돌
        ])


def test_concept_requires_id_and_preferred(tmp_path) -> None:
    bad = tmp_path / "vocab.yaml"
    bad.write_text("version: v1\nconcepts:\n  - preferred: X\n", encoding="utf-8")
    with pytest.raises(TerminologyVocabError):
        TerminologyVocab.from_yaml(bad)


def test_vocab_sha_is_content_sensitive_and_deterministic() -> None:
    a = TerminologyVocab(concepts=[Concept(concept_id="ECCS", preferred="ECCS", uf=("ECC",))])
    b = TerminologyVocab(concepts=[Concept(concept_id="ECCS", preferred="ECCS", uf=("ECC",))])
    c = TerminologyVocab(concepts=[Concept(concept_id="ECCS", preferred="ECCS", uf=("ECC", "ecc2"))])
    assert a.vocab_sha == b.vocab_sha          # 동일 내용 → 동일 해시(결정론).
    assert a.vocab_sha != c.vocab_sha          # 내용 변경 → 해시 변경(audit 감지).


# --- canonicalize (정밀) -----------------------------------------------------
def test_canonicalize_maps_uf_to_preferred_with_definition() -> None:
    v = _repo_vocab()
    r = v.canonicalize(["ECC", "혁신형 소형모듈원자로", "unknown-term"])
    # uf → preferred 치환, 미등록은 원형 passthrough(입력 순서 보존).
    assert r.canonical_terms == ("ECCS", "i-SMR", "unknown-term")
    assert r.unresolved == ("unknown-term",)
    assert r.concept_ids == ("ECCS", "i-SMR")          # 재현 핀.
    assert "Emergency Core Cooling System" in r.definitions["ECCS"]
    assert "비상노심냉각계통" in r.definitions["ECCS"]   # en(ko) 병기.


def test_canonicalize_preferred_term_resolves_to_itself() -> None:
    v = _repo_vocab()
    r = v.canonicalize(["RAI", "fsar"])  # 대소문자 무시.
    assert r.canonical_terms == ("RAI", "FSAR")
    assert r.unresolved == ()


def test_canonicalize_dedups_concept_ids_but_keeps_term_order() -> None:
    v = _repo_vocab()
    r = v.canonicalize(["ECC", "ECCS", "Emergency Core Cooling System"])
    assert r.canonical_terms == ("ECCS", "ECCS", "ECCS")  # 항별 치환은 유지.
    assert r.concept_ids == ("ECCS",)                     # concept 핀은 중복 제거.


# --- expand (재현) -----------------------------------------------------------
def test_expand_returns_uf_synonyms_excluding_self() -> None:
    v = _repo_vocab()
    r = v.expand(["ECCS"], relations=("uf",))
    # uf 동의어 반환, 입력 term/preferred 자신은 제외.
    assert "ECC" in r.expanded_terms
    assert "ECCS" not in r.expanded_terms
    assert r.relations["ECCS"]


def test_expand_unknown_term_yields_nothing() -> None:
    v = _repo_vocab()
    r = v.expand(["does-not-exist"])
    assert r.expanded_terms == ()
    assert r.relations == {}


def test_expand_respects_max_per_term() -> None:
    v = TerminologyVocab(concepts=[
        Concept(concept_id="X", preferred="X", uf=("a", "b", "c", "d")),
    ])
    r = v.expand(["X"], relations=("uf",), max_per_term=2)
    assert len(r.relations["X"]) == 2


def test_expand_rt_is_opt_in() -> None:
    v = TerminologyVocab(concepts=[
        Concept(concept_id="X", preferred="X", uf=("syn",), rt=("related",)),
    ])
    # 기본(uf,nt)엔 rt 미포함 — affinitive 라 drift 위험.
    assert "related" not in v.expand(["X"]).expanded_terms
    # opt-in 하면 포함.
    assert "related" in v.expand(["X"], relations=("uf", "rt")).expanded_terms


# --- default 폴백 ------------------------------------------------------------
def test_default_is_passthrough() -> None:
    v = TerminologyVocab.default()
    r = v.canonicalize(["ECCS", "anything"])
    assert r.canonical_terms == ("ECCS", "anything")  # 빈 어휘 → 전부 passthrough.
    assert r.unresolved == ("ECCS", "anything")
    assert v.expand(["ECCS"]).expanded_terms == ()
