"""operating point(retriever_top_k) → hybrid search_pipeline 선택 계약.

벤치마크가 가중치를 고정 k 에 맞춰 튜닝하므로 pipeline 선택은 config 에서
operating point 로 결정해야 한다(어댑터 per-call top_k 는 fetch 깊이라 부적합).
"""

import pytest

from app.config.profiles import _HYBRID_K_PIPELINES, _resolve_hybrid_pipeline


@pytest.mark.parametrize(
    "top_k,expected",
    [
        (5, "nrc-hybrid-search-k5"),    # weights=[0.4, 0.3, 0.3]
        (10, "nrc-hybrid-search-k10"),  # weights=[0.4, 0.2, 0.4]
        (3, "nrc-hybrid-search"),       # 미벤치마크 k → 폴백
        (20, "nrc-hybrid-search"),      # fetch 깊이 값이 새도 폴백
    ],
)
def test_operating_k_selects_pipeline(top_k, expected):
    assert _resolve_hybrid_pipeline(top_k, "nrc-hybrid-search") == expected


def test_empty_fallback_normalizes_to_none():
    # 미벤치마크 k + 빈 폴백 → None (search_pipeline 미지정).
    assert _resolve_hybrid_pipeline(3, "") is None
    assert _resolve_hybrid_pipeline(3, None) is None


def test_benchmarked_k_ignores_empty_fallback():
    # 벤치마크 k 는 폴백이 비어도 전용 pipeline 을 쓴다.
    assert _resolve_hybrid_pipeline(5, None) == "nrc-hybrid-search-k5"


def test_pipeline_files_match_map():
    # 맵의 모든 pipeline id 는 init.sh 가 등록하는 실제 JSON 파일이어야 한다
    # (stem == pipeline id). 누락 시 운영에서 404 search pipeline.
    from pathlib import Path

    pipe_dir = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "opensearch"
        / "pipelines"
    )
    for name in _HYBRID_K_PIPELINES.values():
        assert (pipe_dir / f"{name}.json").is_file(), f"missing pipeline file: {name}.json"
