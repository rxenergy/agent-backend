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


def _cite(cid: str, *, document_id=None, formatted=None, page=None,
          source_url=None, tables=None, kind="chunk", table_tag=None) -> Citation:
    return Citation(citation_id=cid, document_id=document_id, formatted=formatted,
                    page=page, source_url=source_url, tables=tables,
                    kind=kind, table_tag=table_tag)


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
        _cite("cite-0", document_id="ML18002A422", page=12,
              formatted="[cite-0] [ML18002A422, Section C.I.4, p. 12, Rev. 5]"),
        _cite("cite-1", document_id="RG-1.206", page=3,
              formatted="[cite-1] [RG-1.206, Section 1.1, p. 3, Rev. 2]"),
    ]
    m = {"cite-0": 1, "cite-1": 2}
    out = references_section(cites, m)
    assert "**근거 (References)**" in out
    # 본문 마커와 동일한 [N] 형식([cite-N]·N. 아님).
    assert "[cite-" not in out
    # ADAMS → 마크다운 링크 + #page=N 딥링크(Chrome 이 해당 페이지로 점프).
    assert "[1] [ML18002A422, Section C.I.4, p. 12, Rev. 5]" \
           "(https://www.nrc.gov/docs/ML1800/ML18002A422.pdf#page=12)" in out
    # 비-ADAMS → 평문(링크 없음).
    assert "[2] RG-1.206, Section 1.1, p. 3, Rev. 2" in out
    assert "(http" not in out.split("[2] ")[1]


def test_references_prefers_index_source_url_for_10cfr():
    # 10 CFR(비-ADAMS) 인데 인덱스 source_url(govinfo PDF)이 있으면 평문이 아니라
    # 그 URL 로 링크 + PDF 라서 #page=N 딥링크.
    cites = [_cite(
        "cite-0", document_id="CFR-2024-title10-vol1", page=512,
        source_url="https://www.govinfo.gov/content/pkg/CFR-2024-title10-vol1/pdf/"
                   "CFR-2024-title10-vol1-sec50-46.pdf",
        formatted="[cite-0] [10 CFR §50.46, Section 50.46(b)(1), p. 512] (구속 요건)",
    )]
    out = references_section(cites, {"cite-0": 1})
    assert "[1] [10 CFR §50.46, Section 50.46(b)(1), p. 512]" in out
    assert ("(https://www.govinfo.gov/content/pkg/CFR-2024-title10-vol1/pdf/"
            "CFR-2024-title10-vol1-sec50-46.pdf#page=512)") in out
    assert "(구속 요건)" not in out  # 권위 태그는 References 비노출


def test_references_index_url_non_pdf_omits_page_anchor():
    # HTML detailsLink 류(.pdf 아님)는 #page 앵커를 붙이지 않는다.
    cites = [_cite(
        "cite-0", document_id="CFR-2024-title10-vol1", page=512,
        source_url="https://www.govinfo.gov/app/details/CFR-2024-title10-vol1",
        formatted="[cite-0] [10 CFR §50.46, Section 50.46, p. 512]",
    )]
    out = references_section(cites, {"cite-0": 1})
    assert "(https://www.govinfo.gov/app/details/CFR-2024-title10-vol1)" in out
    assert "#page=" not in out


def test_references_index_url_with_existing_fragment_not_double_anchored():
    # source_url 에 이미 fragment 가 있으면 page 앵커를 덧붙이지 않는다(원본 보존).
    cites = [_cite(
        "cite-0", document_id="ML18002A422", page=12,
        source_url="https://www.nrc.gov/docs/ML1800/ML18002A422.pdf#section=3",
        formatted="[cite-0] [ML18002A422, Section 3, p. 12]",
    )]
    out = references_section(cites, {"cite-0": 1})
    assert "ML18002A422.pdf#section=3)" in out
    assert "#page=12" not in out


def test_references_adams_link_omits_page_anchor_when_no_page():
    # page 결손 시 #page 앵커 없이 URL 만(잘못된 페이지로 보내지 않는다).
    cites = [_cite("cite-0", document_id="ML18002A422", page=None,
                   formatted="[cite-0] [ML18002A422, Section C.I.4, p. ?]")]
    out = references_section(cites, {"cite-0": 1})
    assert "(https://www.nrc.gov/docs/ML1800/ML18002A422.pdf)" in out
    assert "#page=" not in out


def test_references_strips_cite_prefix_and_weight_tag():
    # format_citation 은 inner 대괄호 뒤에 권위 태그 " (…)" 를 붙인다 — References 엔
    # [cite-N] 접두도, 권위 태그(신청자 주장 등)도 노출되지 않고 문서 식별 정보만 남는다.
    cites = [_cite("cite-4", document_id="ML23304A389",
                   formatted="[cite-4] [ML23304A389, Chapter (preamble) > #41, p. 6]"
                             " (신청자 주장)")]
    out = references_section(cites, {"cite-4": 1})
    assert "[cite-4]" not in out
    assert "신청자 주장" not in out  # 권위 태그 비노출
    # 라벨 중간 괄호(preamble)는 보존.
    assert "[1] [ML23304A389, Chapter (preamble) > #41, p. 6]" in out


def test_references_fallback_strips_tag_without_inner_brackets():
    # 예상 밖 형식(inner 대괄호 없음) → fallback 경로도 [cite-N] 접두·권위 태그를 제거.
    cites = [_cite("cite-0", document_id="RG-9", formatted="[cite-0] RG-9, p. 2 (심사 기록)")]
    out = references_section(cites, {"cite-0": 1})
    assert "심사 기록" not in out and "[cite-0]" not in out
    assert "[1] RG-9, p. 2" in out


def test_references_in_body_appearance_order():
    # 본문 등장 순서대로(표시번호순) — cite-9 가 먼저면 [1], cite-2 가 [2].
    cites = [_cite("cite-2", document_id="RG-2", formatted="[cite-2] [RG-2, p. 2]"),
             _cite("cite-9", document_id="RG-9", formatted="[cite-9] [RG-9, p. 9]")]
    out = references_section(cites, {"cite-9": 1, "cite-2": 2})
    lines = [ln for ln in out.splitlines() if ln.startswith("[")]
    assert lines[0].startswith("[1] RG-9") and lines[1].startswith("[2] RG-2")


def test_references_missing_citation_is_visible_not_crash():
    # 본문이 후보에 없는 cite 를 참조(계약 위반) → KeyError 대신 가시 표기.
    out = references_section([], {"cite-3": 1})
    assert "[1] (근거 메타 없음: cite-3)" in out


def test_references_empty_when_no_refs():
    assert references_section([_cite("cite-0")], {}) == ""


# --- References 표 렌더(spec_driven_table_citation_granularity) ---------------
# 입도 분리: kind="table" 인 cite 만 표를 렌더한다. kind="chunk" 는 라벨만(본문 근거).

_GFM_TABLE = "| 항목 | 한계값 |\n| --- | --- |\n| PCT | 2200°F |"


def test_references_renders_table_cite_below_label():
    # 표 cite(kind="table") → 라벨 줄 아래 빈 줄 격리 + caption(bold) + GFM 표.
    cites = [_cite("cite-0", document_id="ML18002A422", page=45, kind="table",
                   table_tag="tb_0001",
                   formatted="[cite-0] [ML18002A422, Section 6.2, p. 45, 표: 표 6.2-1 ECCS 한계값]",
                   tables=[{"tag": "tb_0001", "caption": "표 6.2-1 ECCS 한계값",
                            "markdown": _GFM_TABLE, "html": ""}])]
    out = references_section(cites, {"cite-0": 1})
    # 라벨 ↔ 표 사이 빈 줄(파이프표 격리) + caption bold + 표 본문.
    assert "**표 6.2-1 ECCS 한계값**" in out
    assert _GFM_TABLE in out
    # 라벨 줄(ADAMS 딥링크) 직후 빈 줄로 표가 분리(단락 병합 방지).
    assert "#page=45)\n\n**표 6.2-1 ECCS 한계값**" in out


def test_references_chunk_cite_with_tables_renders_label_only():
    # chunk cite 는 tables 가 실려도(이론상) 표를 렌더하지 않는다 — 본문 근거와 표 근거
    # 분리(선행 "chunk 표 전량 렌더" 폐기). 실제로는 build 가 chunk cite tables=None.
    cites = [_cite("cite-0", document_id="ML18002A422", page=45, kind="chunk",
                   formatted="[cite-0] [ML18002A422, Section 6.2, p. 45]",
                   tables=[{"tag": "t", "caption": "C", "markdown": _GFM_TABLE}])]
    out = references_section(cites, {"cite-0": 1})
    assert _GFM_TABLE not in out
    assert "**C**" not in out
    assert "[1] [ML18002A422, Section 6.2, p. 45]" in out


def test_references_table_cite_html_fallback_when_no_markdown():
    # markdown 비고 html 만 → raw <table> 그대로(marked.js 통과).
    html = "<table><tr><td>PCT</td><td>2200°F</td></tr></table>"
    cites = [_cite("cite-0", document_id="ML1", kind="table",
                   formatted="[cite-0] [ML1, p. 1, 표: t]",
                   tables=[{"tag": "t", "caption": "", "markdown": "", "html": html}])]
    out = references_section(cites, {"cite-0": 1})
    assert html in out


def test_references_mixed_table_and_chunk_isolation():
    # 표 cite 항목은 독립 단락(`\n\n` 격리), chunk cite 라벨은 hard break 단락에 모임.
    cites = [
        _cite("cite-0", document_id="RG-1", formatted="[cite-0] [RG-1, p. 1]"),
        _cite("cite-1", document_id="ML2", kind="table", table_tag="t",
              formatted="[cite-1] [ML2, p. 2, 표: T]",
              tables=[{"tag": "t", "caption": "T", "markdown": _GFM_TABLE}]),
        _cite("cite-2", document_id="RG-3", formatted="[cite-2] [RG-3, p. 3]"),
    ]
    out = references_section(cites, {"cite-0": 1, "cite-1": 2, "cite-2": 3})
    # 표 단락이 인접 라벨과 병합되지 않게 빈 줄로 격리(ML2 는 ADAMS 정규식 미매칭 → 평문).
    assert "\n\n**T**\n\n" in out
    assert _GFM_TABLE in out
    # chunk cite-0/cite-2 라벨은 그대로 존재.
    assert "[1] RG-1, p. 1" in out
    assert "[3] RG-3, p. 3" in out


def test_references_table_cite_empty_body_renders_label_only():
    # 표 본문(markdown·html) 없음 → 라벨만(표 블록 미삽입).
    cites = [_cite("cite-0", document_id="ML1", kind="table",
                   formatted="[cite-0] [ML1, p. 1, 표: t]",
                   tables=[{"tag": "t", "caption": "C", "markdown": "", "html": ""}])]
    out = references_section(cites, {"cite-0": 1})
    assert out == "**근거 (References)**\n\n[1] ML1, p. 1, 표: t"


def test_references_no_tables_attr_is_safe():
    # tables=None(기본 chunk cite) → 기존 출력 불변(회귀 가드).
    cites = [_cite("cite-0", document_id="RG-1", formatted="[cite-0] [RG-1, p. 1]")]
    out = references_section(cites, {"cite-0": 1})
    assert out == "**근거 (References)**\n\n[1] RG-1, p. 1"


def test_references_no_double_link_when_label_already_linked():
    # format_citation 이 ADAMS inner 에 이미 [ML..](url) 링크를 넣으면, References 는
    # 재감싸지 않는다(이중 링크 [[ML..](url), …](url) 방지). 라벨 안 링크가 출처.
    cites = [_cite(
        "cite-0", document_id="ML18002A422", page=45,
        formatted="[cite-0] [[ML18002A422](https://www.nrc.gov/docs/ML1800/"
                  "ML18002A422.pdf#page=45), Chapter 6.2, p. 45]",
    )]
    out = references_section(cites, {"cite-0": 1})
    assert "[[ML" not in out  # 이중 링크 없음
    # 라벨 안의 단일 markdown 링크는 보존.
    assert "[ML18002A422](https://www.nrc.gov/docs/ML1800/ML18002A422.pdf#page=45)" in out


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
    assert "[1] [ML18002A422, p. 1]" in trailer
    assert "[2] RG-9, p. 2" in trailer


# --- 결합 인용(combined markers) — 한 대괄호에 묶인 cite-N 들 -----------------


def test_renumber_and_rewrite_combined_group():
    # 모델이 계약을 어기고 결합 인용을 내도 개별 cite 로 분해·재번호한다.
    m = renumber_map("자연순환[cite-0, cite-2] 과 가압[cite-2]")
    assert m == {"cite-0": 1, "cite-2": 2}
    out = rewrite_inline("자연순환[cite-0, cite-2] 과 가압[cite-2]", m)
    # 결합형 → 분리된 대괄호(OpenWebUI 는 분리 토큰만 링크).
    assert out == "자연순환[1][2] 과 가압[2]"


def test_rewrite_combined_with_semicolon_and_space():
    m = renumber_map("x[cite-1; cite-3] y[cite-0 cite-1]")
    assert m == {"cite-1": 1, "cite-3": 2, "cite-0": 3}
    out = rewrite_inline("x[cite-1; cite-3] y[cite-0 cite-1]", m)
    assert out == "x[1][2] y[3][1]"


def test_stream_rewriter_combined_group_one_token():
    rw = CiteStreamRewriter()
    out = rw.feed("자연순환[cite-0, cite-2] 끝") + rw.flush()
    assert out == "자연순환[1][2] 끝"
    assert rw.renumber == {"cite-0": 1, "cite-2": 2}


def test_stream_rewriter_combined_group_split_across_tokens():
    # advisor 회귀 — 결합형이 토큰 경계로 쪼개져도 닫힘 `]` 전에 raw 로 새지 않는다.
    rw = CiteStreamRewriter()
    out = ""
    for tok in ["문장", "[cite-0,", " cite-", "2]", " 다음", "[cite-1, cite-0]", "."]:
        out += rw.feed(tok)
    out += rw.flush()
    assert out == "문장[1][2] 다음[3][1]."
    assert rw.renumber == {"cite-0": 1, "cite-2": 2, "cite-1": 3}


def test_stream_rewriter_chemistry_bracket_not_overheld():
    # `[cesium-137]` 같은 정상 대괄호는 그룹 알파벳 밖 문자(s)에서 즉시 통과.
    rw = CiteStreamRewriter()
    out = rw.feed("동위원소 [cesium-137] 측정[cite-0].") + rw.flush()
    assert out == "동위원소 [cesium-137] 측정[1]."
    assert rw.renumber == {"cite-0": 1}
