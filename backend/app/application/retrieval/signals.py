from __future__ import annotations

import re

from app.domain.retrieval import RetrievedChunk

# v3.1 Node 6 — 5-신호(G1–G5) 계산기. 전부 순수·결정론·부수효과 없음
# (docs/plans/hierarchical_corrective_workflow.v1.md §6.1). 모델 의존 신호
# (G2 cross-encoder)는 PR-5 미구현 — semantic_signal 은 0.0 을 돌려주고 정책
# weight 0 으로 비활성. 평가자가 신호를 결합/게이팅한다(evaluator.py).

_WORD = re.compile(r"[0-9a-zA-Z가-힣]+")


def tokenize(text: str | None) -> list[str]:
    return _WORD.findall((text or "").lower())


def _chunk_text(chunk: RetrievedChunk) -> str:
    # 평가 대상 본문 — 전체 text 가 있으면 그것, 없으면 snippet.
    return (getattr(chunk, "text", None) or chunk.snippet or "")


# --- G1 Lexical -------------------------------------------------------------


def lexical_coverage(query_text: str, chunk: RetrievedChunk) -> float:
    """질의 토큰 중 chunk 본문에 등장하는 비율(Jaccard-유사 coverage). 규제
    도메인에서 lexical 은 dense 보다 신뢰됨(조문번호·법령명은 의역 불가)."""
    q = set(tokenize(query_text))
    if not q:
        return 0.0
    body = set(tokenize(_chunk_text(chunk)))
    return len(q & body) / len(q)


# --- entity coverage (hard gate 입력) ---------------------------------------


def entity_coverage(entities: dict[str, list[str]] | None, chunk: RetrievedChunk) -> float:
    """추출된 엔티티 값 중 chunk 본문에 등장하는 비율. 빈 entities 면 1.0
    (not-applicable — 게이트가 N/A 처리)."""
    vals = [v for vs in (entities or {}).values() for v in vs if v]
    if not vals:
        return 1.0
    body = _chunk_text(chunk).lower()
    hit = sum(1 for v in vals if v.lower() in body)
    return hit / len(vals)


# --- G3 Regulatory Structural -----------------------------------------------


def version_conflict(chunk: RetrievedChunk, version_constraint: str | None) -> bool | None:
    """version_match 의 음성판정. 둘 다 있을 때만 비교 — chunk.effective_on 이
    질의 effective 기준일보다 *이르면*(예전 개정) 충돌 후보. 입력 부재면 None
    (not-applicable). YYYY-MM-DD 문자열 비교(사전식=시간순)."""
    eo = chunk.effective_on
    if not eo or not version_constraint:
        return None
    return eo < version_constraint


def authority_rank(chunk: RetrievedChunk, rank_map: dict[str, int]) -> int | None:
    t = (chunk.authority_tier or "").lower()
    return rank_map.get(t)


def regulatory_signal(
    chunk: RetrievedChunk,
    *,
    version_constraint: str | None,
    tier_score: dict[str, float],
) -> float:
    """G3 점수 [0,1] — 사용 가능한 하위신호의 평균. v1 에선 authority_tier(=
    collection 유도)만 기여하는 경우가 많다. clause_id resolve / jurisdiction
    매칭은 해당 메타가 있을 때 가산. version 충돌이 확정이면 0(강한 음성)."""
    if version_conflict(chunk, version_constraint) is True:
        return 0.0
    parts: list[float] = []
    tier = (chunk.authority_tier or "").lower()
    if tier in tier_score:
        parts.append(tier_score[tier])
    if chunk.clause_id:
        parts.append(1.0)  # clause_id 가 인덱스에 존재(=resolve 가능)
    if chunk.jurisdiction:
        parts.append(1.0)
    if not parts:
        return 0.0
    return sum(parts) / len(parts)


# --- G4 Ensemble Agreement --------------------------------------------------


def ensemble_signal(chunk_id: str, rrf_scores: dict[str, float], max_rrf: float) -> float:
    """cross-strategy agreement — RRF 점수를 풀 내 최댓값으로 정규화. 여러
    전략이 같은 chunk 를 올리면 RRF 가 높다. 단일 전략 경로에선 순위 단조라
    변별력이 거의 없음(정책 weight 로 보정)."""
    if max_rrf <= 0:
        return 0.0
    return min(1.0, rrf_scores.get(chunk_id, 0.0) / max_rrf)


# --- G5 Confidence Calibration (overall) ------------------------------------


def top_gap(s_totals: list[float]) -> float:
    """top-1 / top-2 점수 격차 — 답이 있다고 *확신* 가능한지(refusal calibration).
    PR-5 에선 overall 진단용 기록만(임계 게이팅은 P4). 후보 <2 면 0."""
    if len(s_totals) < 2:
        return 0.0
    s = sorted(s_totals, reverse=True)
    return max(0.0, s[0] - s[1])
