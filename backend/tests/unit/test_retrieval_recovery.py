from __future__ import annotations

from app.application.retrieval.recovery import RetrievalRecoverer
from app.domain.retrieval import ChunkSignals, EvaluationResult, GateDecision


def _eval(*, entity_coverage=0.9, s_total=0.6, decision="weak") -> EvaluationResult:
    return EvaluationResult(
        per_chunk=(
            ChunkSignals(chunk_id="c1", s_total=s_total, entity_coverage=entity_coverage,
                         decision=decision),
        ),
        overall_decision=decision,
    )


def test_diagnose_entity_coverage_low():
    r = RetrievalRecoverer({}, entity_coverage_min=0.3)
    assert r.diagnose(_eval(entity_coverage=0.1)) == "entity_coverage_low"


def test_diagnose_low_scores():
    r = RetrievalRecoverer({})
    assert r.diagnose(_eval(entity_coverage=0.9, s_total=0.2)) == "low_scores"


def test_diagnose_generic_when_scores_ok():
    r = RetrievalRecoverer({})
    assert r.diagnose(_eval(entity_coverage=0.9, s_total=0.8)) == "generic"


def test_diagnose_no_results_on_empty():
    assert RetrievalRecoverer({}).diagnose(EvaluationResult()) == "no_results"


def test_entity_coverage_action_expands_synonyms():
    r = RetrievalRecoverer({"NuScale": ["NPM"]})
    action = r.plan_action(
        "entity_coverage_low",
        entities={"reactor_type": ["NuScale"]}, fetch_k=20, min_score=0.1,
    )
    assert action.strategy_id == "synonym_expand"
    assert "NPM" in action.entities["reactor_type"]
    assert action.fetch_k == 20  # 동의어 확장은 fetch_k 유지


def test_entity_coverage_action_falls_back_to_relax_without_synonyms():
    """동의어 없으면 동일검색 무한반복 방지를 위해 filter 완화로 폴백."""
    r = RetrievalRecoverer({})  # 빈 사전
    action = r.plan_action(
        "entity_coverage_low",
        entities={"reactor_type": ["UnknownTerm"]}, fetch_k=20, min_score=0.5,
    )
    assert action.strategy_id == "relax_filter"
    assert action.fetch_k == 40 and action.min_score == 0.0


def test_low_scores_action_relaxes_filter():
    r = RetrievalRecoverer({})
    action = r.plan_action("low_scores", entities={}, fetch_k=20, min_score=0.5)
    assert action.strategy_id == "relax_filter"
    assert action.fetch_k == 40 and action.min_score == 0.0


def test_default_recoverer_has_two_rounds():
    assert RetrievalRecoverer.default().max_rounds == 2
