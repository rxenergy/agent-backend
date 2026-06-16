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


def test_expand_tables_same_tag_accumulates_in_order() -> None:
    # 같은 tag 의 엔트리가 여러 개면 배열 순서대로 누적 결합(덮어쓰지 않음).
    tables = [
        {"tag": "tb_0001", "markdown": "FIRST"},
        {"tag": "tb_0001", "caption": "둘째", "markdown": "SECOND"},
    ]
    assert _expand_tables("x [TABLE: tb_0001] y", tables) == "x FIRST\n\n**둘째**\n\nSECOND y"


def test_expand_tables_same_tag_skips_empty_entries() -> None:
    # 누적 시 markdown 없는 엔트리는 건너뛰고 유효한 것만 결합.
    tables = [
        {"tag": "tb_0001", "caption": "C"},  # markdown 없음 → skip
        {"tag": "tb_0001", "markdown": "ONLY"},
    ]
    assert _expand_tables("a [TABLE: tb_0001] b", tables) == "a ONLY b"


def test_expand_tables_duplicate_marker_renders_once() -> None:
    # 본문에 같은 tag 마커가 여러 번 나오면 표는 첫 마커에만 싣고 이후 마커는 제거.
    tables = [{"tag": "tb_0001", "markdown": "TBL"}]
    out = _expand_tables("a [TABLE: tb_0001] b [TABLE: tb_0001] c", tables)
    assert out == "a TBL b  c"
    assert out.count("TBL") == 1


def test_expand_tables_distinct_tags_each_render() -> None:
    # 서로 다른 tag 의 마커는 각각 한 번씩 렌더(중복 제거는 tag 단위).
    tables = [{"tag": "tb_0001", "markdown": "T1"}, {"tag": "tb_0002", "markdown": "T2"}]
    out = _expand_tables("[TABLE: tb_0001] [TABLE: tb_0002] [TABLE: tb_0001]", tables)
    assert out == "T1 T2 "


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


def test_full_mode_table_goes_to_tables_block_not_body() -> None:
    # 입도 분리(D1): 표는 본문에서 빠지고 # TABLES 블록에 별도 cite 로 렌더.
    table = {
        "tag": "tb_0001",
        "caption": ("PROBABILITY OF TURBINE FAILURE RESULTING IN THE EJECTION OF "
                    "TURBINE ROTOR FRAGMENTS (P₁) AND RECOMMENDED LICENSEE ACTIONS"),
        "html": "",
        "markdown": ("| Case | FAVORABLE | UNFAVORABLE | ACTION |\n| --- | --- | --- | --- |\n"
                     "| A | P₁ < 10⁻⁴ | P₁ < 10⁻⁵ | minimum reliability |"),
    }
    ctx = _render([_chunk(text="터빈 미사일 보호 [TABLE: tb_0001] 이후", tables=[table])])
    assert "[TABLE:" not in ctx  # 본문 마커 제거됨(인라인 치환 아님)
    # 표는 # TABLES 블록에 독립 cite 로.
    assert "# TABLES" in ctx
    assert "**PROBABILITY OF TURBINE FAILURE" in ctx  # caption(bold)
    assert "| Case | FAVORABLE | UNFAVORABLE | ACTION |" in ctx  # markdown 표 헤더
    assert "P₁ < 10⁻⁴" in ctx  # 표 값 verbatim
    # 본문 cite-0(chunk) + 표 cite-1(table) — 통합 풀.
    assert "[cite-0]" in ctx and "[cite-1] (표" in ctx


def test_snippets_mode_table_also_separated() -> None:
    ctx = _render([
        _chunk(snippet="요건 [TABLE: tb_0001] 끝",
               tables=[{"tag": "tb_0001", "markdown": "표내용"}]),
    ], mode="snippets")
    assert "표내용" in ctx and "[TABLE:" not in ctx
    assert "# TABLES" in ctx


def test_full_mode_no_tables_marker_stripped_no_tables_block() -> None:
    # 표 데이터 없이 마커만 있으면(엣지) 본문 마커는 제거, # TABLES 블록은 미생성.
    ctx = _render([_chunk(text="요건 [TABLE: tb_0001] 끝", tables=None)])
    assert "[TABLE: tb_0001]" not in ctx  # 깨진 마커를 본문에 남기지 않음
    assert "# TABLES" not in ctx  # 렌더할 표 cite 없음


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


# --- 입도 분리: chunk 본문 cite + 표별 table cite(spec_driven_table_citation_granularity)
def test_build_splits_chunk_and_table_into_separate_cites() -> None:
    # 표 2개 chunk → cite 3개: 본문 cite-0(chunk) + table cite-1/cite-2(통합 풀).
    tables = [
        {"tag": "tb_0001", "caption": "C1", "markdown": "| a |", "html": ""},
        {"tag": "tb_0002", "caption": "C2", "markdown": "| b |", "html": ""},
    ]
    b = ContextBuilder(capture_mode="full")
    pack = b.build(
        interaction_id="i", query_text="q", chat_history=(),
        conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
        entities={}, chunks=[_chunk(chunk_id="ch0",
                                    text="x [TABLE: tb_0001][TABLE: tb_0002] y",
                                    tables=tables)],
    )
    cands = pack.citation_candidates
    assert len(cands) == 3
    # 본문 cite — kind=chunk, 표 자동 렌더 안 함(tables=None).
    assert cands[0].citation_id == "cite-0"
    assert cands[0].kind == "chunk" and cands[0].tables is None
    # 표 cite — kind=table, parent 승계, 단일 표.
    assert cands[1].citation_id == "cite-1"
    assert cands[1].kind == "table" and cands[1].parent_chunk_id == "ch0"
    assert cands[1].table_tag == "tb_0001" and cands[1].tables == [tables[0]]
    assert cands[2].citation_id == "cite-2" and cands[2].table_tag == "tb_0002"
    # 표 cite 출처 메타는 parent chunk 승계(D3).
    assert cands[1].document_id == cands[0].document_id
    # 표 cite 라벨에 표 식별(caption) 포함.
    assert "표: C1" in (cands[1].formatted or "")


def test_build_table_cite_skips_empty_table_body() -> None:
    # markdown·html 둘 다 빈 표는 cite 로 승격하지 않는다(인용 실체 없음).
    tables = [{"tag": "tb_0001", "caption": "C", "markdown": "", "html": ""}]
    b = ContextBuilder(capture_mode="full")
    pack = b.build(
        interaction_id="i", query_text="q", chat_history=(),
        conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
        entities={}, chunks=[_chunk(text="x [TABLE: tb_0001] y", tables=tables)],
    )
    assert len(pack.citation_candidates) == 1  # 본문 cite 만
    assert pack.citation_candidates[0].kind == "chunk"


def test_build_no_tables_single_chunk_cite() -> None:
    b = ContextBuilder(capture_mode="full")
    pack = b.build(
        interaction_id="i", query_text="q", chat_history=(),
        conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
        entities={}, chunks=[_chunk(text="no tables")],
    )
    assert len(pack.citation_candidates) == 1
    assert pack.citation_candidates[0].kind == "chunk"
    assert pack.citation_candidates[0].tables is None
