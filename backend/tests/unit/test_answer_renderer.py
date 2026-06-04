"""answer_renderer: structured AgentResponse → OpenWebUI-friendly markdown body.
Pure-function + streaming-sanitizer tests."""
from __future__ import annotations

from app.api.answer_renderer import (
    CiteStreamRewriter,
    answer_trailer,
    caveat_callouts,
    compose_answer_body,
    references_section,
    renumber_map,
    rewrite_inline,
)
from app.application.context.citation_format import adams_url
from app.domain.interaction import AgentResponse, Citation


def _cite(cid: str, *, document_id=None, formatted=None) -> Citation:
    return Citation(citation_id=cid, document_id=document_id, formatted=formatted)


def _resp(
    answer_text: str,
    *,
    citations=(),
    refusal_reason=None,
    verification_status="pass",
    regulatory_grounding="n_a",
) -> AgentResponse:
    return AgentResponse(
        interaction_id="i",
        answer_text=answer_text,
        citations=tuple(citations),
        refusal_reason=refusal_reason,
        verification_status=verification_status,
        scenario_object="O1",
        scenario_depth="D2",
        latency_ms=1,
        regulatory_grounding=regulatory_grounding,
    )


# --- ADAMS URL derivation --------------------------------------------------


def test_adams_url_derives_for_ml_accession():
    assert adams_url("ML18002A422") == "https://www.nrc.gov/docs/ML1800/ML18002A422.pdf"
    assert adams_url("ML102940118") == "https://www.nrc.gov/docs/ML1029/ML102940118.pdf"


def test_adams_url_none_for_non_adams():
    assert adams_url("RG-1.206") is None
    assert adams_url("KEPIC-ENB") is None
    assert adams_url(None) is None
    # 앵커 — ML-부분열을 포함하는 non-ADAMS id 는 매칭하지 않는다.
    assert adams_url("doc-ML1800-x") is None


# --- renumber / inline rewrite ---------------------------------------------


def test_renumber_first_appearance():
    m = renumber_map("a [cite-7] b [cite-2] c [cite-7] d")
    assert m == {"cite-7": 1, "cite-2": 2}


def test_rewrite_inline_uses_display_numbers():
    m = renumber_map("x [cite-7] y [cite-2]")
    assert rewrite_inline("x [cite-7] y [cite-2]", m) == "x [1] y [2]"
    # 맵에 없는 cite-id 는 원형 유지(계약 위반 방어).
    assert rewrite_inline("z [cite-9]", m) == "z [cite-9]"


# --- references section ----------------------------------------------------


def test_references_links_adams_and_plain_fallback():
    cites = [
        _cite("cite-0", document_id="ML18002A422",
              formatted="[cite-0] [ML18002A422, Section C.I.4, p. 12, Rev. 5]"),
        _cite("cite-1", document_id="RG-1.206",
              formatted="[cite-1] [RG-1.206, Section 1.1, p. 3, Rev. 2]"),
    ]
    m = {"cite-0": 1, "cite-1": 2}
    out = references_section(cites, m)
    assert "**근거 (References)**" in out
    # ADAMS → 마크다운 링크.
    assert "1. [ML18002A422, Section C.I.4, p. 12, Rev. 5]" \
           "(https://www.nrc.gov/docs/ML1800/ML18002A422.pdf)" in out
    # 비-ADAMS → 평문(링크 없음).
    assert "2. RG-1.206, Section 1.1, p. 3, Rev. 2" in out
    assert "(http" not in out.split("2. ")[1]


def test_references_missing_citation_is_visible_not_crash():
    # 본문이 후보에 없는 cite 를 참조(계약 위반) → KeyError 대신 가시 표기.
    out = references_section([], {"cite-3": 1})
    assert "1. (근거 메타 없음: cite-3)" in out


def test_references_empty_when_no_refs():
    assert references_section([_cite("cite-0")], {}) == ""


# --- caveat callouts -------------------------------------------------------


def test_caveat_callouts_partial_and_regulatory():
    r = _resp("x", verification_status="PARTIAL", regulatory_grounding="unverified")
    out = caveat_callouts(r)
    assert "**부분 답변**" in out and "**규제 근거 미검증**" in out
    assert out.count(">") >= 2  # blockquote 2개


def test_caveat_callouts_none_when_clean():
    assert caveat_callouts(_resp("x", verification_status="pass")) == ""


# --- compose (non-streaming) -----------------------------------------------


def test_compose_full_body_with_refs_and_callout():
    cites = [_cite("cite-0", document_id="ML18002A422",
                   formatted="[cite-0] [ML18002A422, Section C.I.4, p. 12, Rev. 5]")]
    r = _resp("자연순환을 쓴다[cite-0].", citations=cites,
              regulatory_grounding="unverified")
    out = compose_answer_body(r)
    assert out.startswith("자연순환을 쓴다[1].")     # 인라인 재번호
    assert "**규제 근거 미검증**" in out               # callout
    assert "**근거 (References)**" in out              # references
    assert "https://www.nrc.gov/docs/ML1800/ML18002A422.pdf" in out


def test_compose_hard_refusal_is_message_only():
    r = _resp("관련 정보를 찾을 수 없습니다.", refusal_reason="retrieval_no_result",
              verification_status="fail", regulatory_grounding="unverified")
    out = compose_answer_body(r)
    assert out == "관련 정보를 찾을 수 없습니다."       # 거부 메시지만 — refs/callout 없음
    assert "근거 (References)" not in out
    assert "규제 근거 미검증" not in out


def test_compose_partial_is_soft_keeps_body_and_callout():
    r = _resp("초안[cite-0].", citations=[_cite("cite-0", document_id="RG-1",
              formatted="[cite-0] [RG-1, Section 1, p. 1, Rev. 1]")],
              refusal_reason="partial_answer", verification_status="PARTIAL")
    out = compose_answer_body(r)
    assert out.startswith("초안[1].")
    assert "**부분 답변**" in out
    assert "근거 (References)" in out


# --- streaming sanitizer ---------------------------------------------------


def test_stream_rewriter_across_token_boundaries():
    rw = CiteStreamRewriter()
    # [cite-0] 가 여러 델타에 쪼개져 도착.
    out = ""
    for tok in ["자연순환", "을 쓴다", "[ci", "te-", "0]", " 끝", "[cite-1]."]:
        out += rw.feed(tok)
    out += rw.flush()
    assert out == "자연순환을 쓴다[1] 끝[2]."
    assert rw.renumber == {"cite-0": 1, "cite-1": 2}


def test_stream_rewriter_does_not_overhold_normal_brackets():
    rw = CiteStreamRewriter()
    out = rw.feed("배열 [1] 과 [foo] 그리고 ") + rw.feed("[cite-0]")
    out += rw.flush()
    assert out == "배열 [1] 과 [foo] 그리고 [1]"
    assert rw.renumber == {"cite-0": 1}


def test_stream_rewriter_trailing_incomplete_flushed_verbatim():
    rw = CiteStreamRewriter()
    out = rw.feed("문장 [cite-") + rw.flush()
    # 미완성 마커는 그대로 방출(데이터 손실 없음).
    assert out == "문장 [cite-"


def test_stream_renumber_feeds_trailer():
    rw = CiteStreamRewriter()
    rw.feed("a[cite-5] b[cite-2]")
    rw.flush()
    cites = [_cite("cite-5", document_id="ML18002A422",
                   formatted="[cite-5] [ML18002A422, p. 1]"),
             _cite("cite-2", document_id="RG-9",
                   formatted="[cite-2] [RG-9, p. 2]")]
    r = _resp("a[cite-5] b[cite-2]", citations=cites)
    trailer = answer_trailer(r, rw.renumber)
    # 스트리밍 renumber 가 trailer References 번호와 일치(1=cite-5, 2=cite-2).
    assert "1. [ML18002A422, p. 1]" in trailer
    assert "2. RG-9, p. 2" in trailer
