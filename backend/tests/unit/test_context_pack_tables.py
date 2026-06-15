from __future__ import annotations

from app.application.context.pack import (
    ContextBuilder,
    _expand_tables,
    _render_table_entry,
)
from app.domain.retrieval import RetrievedChunk

# spec_driven_table_inline_expansion — 본문의 [TABLE: tag] 마커를 chunk.tables 배열의
# 매칭 엔트리(caption+markdown)로 인라인 치환해 N4 생성 컨텍스트에 표가 실리는지 검증.


# --- _render_table_entry (caption + markdown 결합) --------------------------
def test_render_table_entry_caption_and_markdown() -> None:
    out = _render_table_entry({"tag": "tb_0001", "caption": "표 제목",
                               "markdown": "| a | b |", "html": ""})
    assert out == "**표 제목**\n\n| a | b |"


def test_render_table_entry_markdown_only_when_no_caption() -> None:
    assert _render_table_entry({"tag": "t", "markdown": "| a |", "caption": ""}) == "| a |"
    assert _render_table_entry({"tag": "t", "markdown": "| a |"}) == "| a |"


def test_render_table_entry_missing_markdown_returns_none() -> None:
    assert _render_table_entry({"tag": "t", "caption": "C"}) is None  # markdown 없음
    assert _render_table_entry({"tag": "t", "markdown": ""}) is None  # markdown 빈값
    assert _render_table_entry("not a dict") is None


# --- _expand_tables (배열에서 tag 로 조회) ----------------------------------
def test_expand_tables_matches_tag_in_array() -> None:
    body = "before [TABLE: tb_0001] mid [TABLE: tb_0002] end"
    tables = [
        {"tag": "tb_0001", "markdown": "T1"},
        {"tag": "tb_0002", "caption": "C2", "markdown": "T2"},
    ]
    assert _expand_tables(body, tables) == "before T1 mid **C2**\n\nT2 end"


def test_expand_tables_marker_whitespace_variants() -> None:
    tables = [{"tag": "tb_0001", "markdown": "T1"}]
    assert _expand_tables("x [TABLE:tb_0001] y", tables) == "x T1 y"
    assert _expand_tables("x [TABLE:   tb_0001   ] y", tables) == "x T1 y"


def test_expand_tables_none_or_empty_returns_body_unchanged() -> None:
    body = "no tables [TABLE: tb_0001]"
    assert _expand_tables(body, None) == body
    assert _expand_tables(body, []) == body
    # tag 키 없는 엔트리만 있으면 by_tag 가 비어 body 불변.
    assert _expand_tables(body, [{"markdown": "x"}]) == body


def test_expand_tables_missing_tag_preserves_marker() -> None:
    # 미매칭 마커는 보존(silent 삭제 금지 — 표 누락 가시화, CLAUDE.md #6).
    tables = [{"tag": "tb_0001", "markdown": "T1"}]
    assert _expand_tables("a [TABLE: tb_9999] b", tables) == "a [TABLE: tb_9999] b"


def test_expand_tables_empty_markdown_preserves_marker() -> None:
    tables = [{"tag": "tb_0001", "markdown": "", "caption": "C"}]
    assert _expand_tables("a [TABLE: tb_0001] b", tables) == "a [TABLE: tb_0001] b"


# --- render_for_prompt 통합 (full 모드 — text 전문 + 표 치환) ----------------
def _chunk(**kw) -> RetrievedChunk:
    base = dict(chunk_id="ch", document_id="doc", score=0.9)
    base.update(kw)
    return RetrievedChunk(**base)


def _render(chunks, mode: str = "full") -> str:
    b = ContextBuilder(capture_mode=mode)
    pack = b.build(
        interaction_id="i", query_text="q", chat_history=(),
        conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
        entities={}, chunks=chunks,
    )
    return b.render_for_prompt(pack)


def test_full_mode_expands_real_table_with_caption_and_markdown() -> None:
    # 사용자 실제 데이터 형태(tag/caption/markdown/html 배열).
    table = {
        "tag": "tb_0001",
        "caption": ("PROBABILITY OF TURBINE FAILURE RESULTING IN THE EJECTION OF "
                    "TURBINE ROTOR FRAGMENTS (P₁) AND RECOMMENDED LICENSEE ACTIONS"),
        "html": "",
        "markdown": ("| Case | FAVORABLE | UNFAVORABLE | ACTION |\n| --- | --- | --- | --- |\n"
                     "| A | P₁ < 10⁻⁴ | P₁ < 10⁻⁵ | minimum reliability |"),
    }
    ctx = _render([_chunk(text="터빈 미사일 보호 [TABLE: tb_0001] 이후", tables=[table])])
    assert "[TABLE:" not in ctx  # 마커 치환됨
    assert "**PROBABILITY OF TURBINE FAILURE" in ctx  # caption(bold)
    assert "| Case | FAVORABLE | UNFAVORABLE | ACTION |" in ctx  # markdown 표 헤더
    assert "P₁ < 10⁻⁴" in ctx  # 표 값 verbatim


def test_snippets_mode_also_expands_within_cap() -> None:
    ctx = _render([
        _chunk(snippet="요건 [TABLE: tb_0001] 끝",
               tables=[{"tag": "tb_0001", "markdown": "표내용"}]),
    ], mode="snippets")
    assert "표내용" in ctx and "[TABLE:" not in ctx


def test_full_mode_no_tables_leaves_marker_visible() -> None:
    ctx = _render([_chunk(text="요건 [TABLE: tb_0001] 끝", tables=None)])
    assert "[TABLE: tb_0001]" in ctx


# --- to_snapshot (full 모드 — 치환 전 원본 text + tables 보존) ---------------
def test_snapshot_full_mode_preserves_original_text_and_tables() -> None:
    b = ContextBuilder(capture_mode="full")
    tables = [{"tag": "tb_0001", "caption": "C", "markdown": "MD", "html": ""}]
    c = _chunk(text="body [TABLE: tb_0001] tail", tables=tables)
    pack = b.build(
        interaction_id="i", query_text="q", chat_history=(),
        conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
        entities={}, chunks=[c],
    )
    snap = b.to_snapshot(pack)
    # 스냅샷에는 치환 *전* 원본이 남아 로직 변경 시에도 재현 가능(원칙 5).
    assert snap["chunks"][0]["text"] == "body [TABLE: tb_0001] tail"
    assert snap["chunks"][0]["tables"] == tables
