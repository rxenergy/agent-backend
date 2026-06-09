from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.tools.confidence_scope import ConfidenceScopeTool
from app.application.retrieval.corpus_map import CorpusMap
from app.application.terminology.vocab import TerminologyVocab
from app.ports.tool import ToolExecutionContext

_ROOT = Path(__file__).resolve().parents[3]
_VOCAB = _ROOT / "tools" / "terminology" / "vocab.yaml"
_CORPUS = _ROOT / "tools" / "corpus_map.yaml"

_CTX = ToolExecutionContext(
    interaction_id="i", trace_id="", app_profile="local",
    agent_variant="react_minimal_v1",
)


def _tool() -> ConfidenceScopeTool:
    return ConfidenceScopeTool(
        corpus_map=CorpusMap.from_yaml(_CORPUS),
        vocab=TerminologyVocab.from_yaml(_VOCAB),
        tau_high=0.6, tau_low=0.3,
    )


@pytest.mark.asyncio
async def test_known_term_resolves_with_high_coverage() -> None:
    out = (await _tool().invoke(
        {"query_text": "i-SMR ECCS requirement", "terms": ["ECCS"]}, _CTX)).output
    assert out["term_coverage"] == 1.0
    assert "ECCS" in out["resolved_terms"]
    assert "ECCS" in out["concept_ids"]
    assert out["unresolved_terms"] == []


@pytest.mark.asyncio
async def test_unknown_term_surfaces_as_gap() -> None:
    out = (await _tool().invoke(
        {"query_text": "q", "terms": ["ECCS", "zzz-unknown"]}, _CTX)).output
    assert "zzz-unknown" in out["unresolved_terms"]   # 모델이 메워야 할 공백.
    assert out["term_coverage"] == 0.5
    assert out["signal"] in {"in_scope_high", "in_scope_low_terms"}


@pytest.mark.asyncio
async def test_reproducibility_pins_present() -> None:
    out = (await _tool().invoke({"query_text": "q", "terms": ["ECCS"]}, _CTX)).output
    assert out["vocab_sha"]
    assert out["corpus_map_hash"]
    assert out["known_collections"]   # corpus_map.yaml 의 collection 목록.


@pytest.mark.asyncio
async def test_empty_defaults_are_graceful() -> None:
    # 자산 미배치(default) 폴백 — coverage 0, signal uncertain, 예외 없음.
    tool = ConfidenceScopeTool(corpus_map=CorpusMap.default(), vocab=TerminologyVocab.default())
    out = (await tool.invoke({"query_text": "anything", "terms": ["ECCS"]}, _CTX)).output
    assert out["term_coverage"] == 0.0
    assert out["signal"] == "uncertain"
    assert out["known_collections"] == []


@pytest.mark.asyncio
async def test_entities_flattened_when_terms_absent() -> None:
    out = (await _tool().invoke(
        {"query_text": "q", "entities": {"system": ["ECCS"], "reactor": ["i-SMR"]}}, _CTX)).output
    assert out["term_coverage"] == 1.0
    assert set(out["concept_ids"]) == {"ECCS", "i-SMR"}
