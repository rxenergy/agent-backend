"""verify_slot 직전 동일내용 청크 최신버전 dedup — `dedupe_latest_version` 단위 검증.

본문이 글자 그대로 같은 청크 그룹은 최신판 1개로 접는다(CFR 연도판 등). 최신성 우선순위:
response_date(YYYY-MM-DD) → packageId 연도(source_id) → score. 메타 전무면 score 최고 1개.
컨테이너 불필요 — 도메인 모델(RetrievedChunk)만 사용한다.
"""
from __future__ import annotations

from app.application.agents.slot_pipeline import dedupe_latest_version
from app.domain.retrieval import RetrievedChunk


def _chunk(chunk_id: str, *, text: str | None = None, snippet: str | None = None,
           score: float = 0.5, response_date: str | None = None,
           source_id: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=source_id or chunk_id, score=score,
        text=text, snippet=snippet, response_date=response_date, source_id=source_id,
    )


def _ids(chunks: list[RetrievedChunk]) -> list[str]:
    return [c.chunk_id for c in chunks]


def test_same_body_keeps_latest_response_date() -> None:
    """동일 본문 + response_date 차이 → 최신 date 청크만 남는다."""
    old = _chunk("old", text="같은 조문 본문", response_date="1997-01-01", score=0.9)
    new = _chunk("new", text="같은 조문 본문", response_date="2025-01-01", score=0.2)
    out = dedupe_latest_version([old, new])
    assert _ids(out) == ["new"]


def test_same_body_no_date_uses_packageid_year() -> None:
    """동일 본문 + date 없음, packageId 연도 차이 → 최신 연도(source_id)만."""
    old = _chunk("c1", text="CFR 조문", source_id="CFR-1997-title10-vol1", score=0.9)
    new = _chunk("c2", text="CFR 조문", source_id="CFR-2025-title10-vol1", score=0.1)
    out = dedupe_latest_version([old, new])
    assert _ids(out) == ["c2"]


def test_same_body_no_meta_keeps_highest_score() -> None:
    """동일 본문 + 최신성 메타 전무 → score 최고 1개만(사용자 결정)."""
    a = _chunk("a", text="동일", score=0.3)
    b = _chunk("b", text="동일", score=0.8)
    c = _chunk("c", text="동일", score=0.5)
    out = dedupe_latest_version([a, b, c])
    assert _ids(out) == ["b"]


def test_different_body_keeps_all() -> None:
    """본문이 다르면 전부 보존(병합 안 함)."""
    a = _chunk("a", text="조문 A")
    b = _chunk("b", text="조문 B")
    out = dedupe_latest_version([a, b])
    assert _ids(out) == ["a", "b"]


def test_empty_body_never_merges() -> None:
    """본문 빈 청크 2개(text/snippet 모두 비어있음) → 오병합 없이 둘 다 보존."""
    a = _chunk("a", text=None, snippet=None)
    b = _chunk("b", text="", snippet="")
    out = dedupe_latest_version([a, b])
    assert _ids(out) == ["a", "b"]


def test_normalization_whitespace_and_case() -> None:
    """공백/대소문자만 다른 동일 본문 → 1개로 병합(정규화 확인)."""
    a = _chunk("a", text="Same  Body\nText", response_date="2000-01-01")
    b = _chunk("b", text="same body text", response_date="2020-01-01")
    out = dedupe_latest_version([a, b])
    assert _ids(out) == ["b"]


def test_snippet_fallback_when_no_text() -> None:
    """text 없으면 snippet 으로 동일성 판정."""
    a = _chunk("a", snippet="본문", response_date="2001-01-01")
    b = _chunk("b", snippet="본문", response_date="2002-01-01")
    out = dedupe_latest_version([a, b])
    assert _ids(out) == ["b"]


def test_exception_safe_with_none_metadata() -> None:
    """source_id/response_date None 혼재 입력에서 throw 없이 동작 + 원순서 보존."""
    a = _chunk("a", text="X", source_id=None, response_date=None, score=0.1)
    b = _chunk("b", text="Y", source_id=None, response_date=None, score=0.2)
    c = _chunk("c", text="X", source_id="CFR-2010-title10-vol1", response_date=None, score=0.05)
    out = dedupe_latest_version([a, b, c])
    # "X" 그룹: c(packageId 연도 2010 > a 의 0) 가 이김. "Y" 그룹: b 단독.
    assert _ids(out) == ["b", "c"]


def test_near_dup_punctuation_only_merges() -> None:
    """띄어쓰기·문장부호만 다른 본문(>=0.95) → 최신 1개로 병합."""
    a = _chunk("a", text="The reactor coolant system shall be designed to withstand.",
               response_date="2010-01-01")
    b = _chunk("b", text="The reactor coolant system shall be designed to withstand",
               response_date="2020-01-01")
    out = dedupe_latest_version([a, b])
    assert _ids(out) == ["b"]


def test_near_dup_numeric_revision_keeps_latest() -> None:
    """revision 에 따라 수치만 달라진 동일 문장(>=0.95) → 최신 청크만 남고 오래된 값 제거."""
    old = _chunk("old", text="Containment design pressure shall not exceed 1034 kPa gauge.",
                 response_date="1997-01-01")
    new = _chunk("new", text="Containment design pressure shall not exceed 1138 kPa gauge.",
                 response_date="2025-01-01")
    out = dedupe_latest_version([old, new])
    assert _ids(out) == ["new"]


def test_below_threshold_keeps_both() -> None:
    """본문이 절반가량 다르면(<0.95) 병합하지 않고 둘 다 보존."""
    a = _chunk("a", text="The reactor coolant system shall be designed to withstand loads.")
    b = _chunk("b", text="Emergency core cooling must remain operable after a transient.")
    out = dedupe_latest_version([a, b])
    assert _ids(out) == ["a", "b"]


def test_length_gate_keeps_superset_separate() -> None:
    """한 청크가 다른 청크 본문에 긴 추가문단을 포함(길이비 <0.95) → 별개로 보존."""
    short = _chunk("short", text="Containment design pressure shall not exceed 1138 kPa.")
    long = _chunk(
        "long",
        text=(
            "Containment design pressure shall not exceed 1138 kPa. "
            + "Additional analysis covering thermal hydraulic transients, structural "
            "margins, and post-accident monitoring is required for the licensing basis "
            "and must be documented in the safety analysis report appendices in full."
        ),
    )
    out = dedupe_latest_version([short, long])
    assert _ids(out) == ["short", "long"]


def test_three_near_dups_collapse_to_latest() -> None:
    """near-dup 3개 클러스터 → 최신 1개만 남는다(다중 멤버 클러스터)."""
    a = _chunk("a", text="The setpoint value shall be 100 percent of rated flow.",
               response_date="2000-01-01")
    b = _chunk("b", text="The setpoint value shall be 101 percent of rated flow.",
               response_date="2010-01-01")
    c = _chunk("c", text="The setpoint value shall be 102 percent of rated flow.",
               response_date="2020-01-01")
    out = dedupe_latest_version([a, b, c])
    assert _ids(out) == ["c"]


def test_empty_input() -> None:
    assert dedupe_latest_version([]) == []
