from __future__ import annotations

from app.application.context.pack import (
    ContextBuilder,
    _expand_tables,
    _render_table_entry,
)
from app.domain.retrieval import RetrievedChunk

# spec_driven_table_inline_expansion — 본문의 [TABLE: tb_xxxx] 마커를 chunk.tables 의
# 표 텍스트로 인라인 치환해 N4 생성 컨텍스트에 표 내용이 실리는지 검증한다.


# --- _render_table_entry (D8: text 단독, caption 무시) ----------------------
def test_render_table_entry_text_only() -> None:
    assert _render_table_entry({"text": "TBL"}) == "TBL"
    # caption/title 이 있어도 붙이지 않는다 — text 만.
    assert _render_table_entry({"text": "TBL", "caption": "C", "title": "T"}) == "TBL"


def test_render_table_entry_string_fallback() -> None:
    # entry 가 (dict 아닌) 문자열이면 그대로 폴백 — _source 키 구조 미확정 방어.
    assert _render_table_entry("raw table text") == "raw table text"


def test_render_table_entry_missing_or_empty_returns_none() -> None:
    assert _render_table_entry({"caption": "C"}) is None  # text 키 없음
    assert _render_table_entry({"text": ""}) is None  # text 비어 있음
    assert _render_table_entry(123) is None  # 타입 미지원
    assert _render_table_entry("") is None


# --- _expand_tables ---------------------------------------------------------
def test_expand_tables_single_and_multiple_markers() -> None:
    body = "before [TABLE: tb_0001] mid [TABLE: tb_0002] end"
    tables = {"tb_0001": {"text": "T1"}, "tb_0002": {"text": "T2"}}
    assert _expand_tables(body, tables) == "before T1 mid T2 end"


def test_expand_tables_marker_whitespace_variants() -> None:
    tables = {"tb_0001": {"text": "T1"}}
    assert _expand_tables("x [TABLE:tb_0001] y", tables) == "x T1 y"
    assert _expand_tables("x [TABLE:   tb_0001   ] y", tables) == "x T1 y"


def test_expand_tables_none_returns_body_unchanged() -> None:
    body = "no tables here [TABLE: tb_0001]"
    assert _expand_tables(body, None) == body


def test_expand_tables_missing_id_preserves_marker() -> None:
    # 미매칭 마커는 보존(silent 삭제 금지 — 표 누락 가시화, CLAUDE.md #6).
    tables = {"tb_0001": {"text": "T1"}}
    assert _expand_tables("a [TABLE: tb_9999] b", tables) == "a [TABLE: tb_9999] b"


def test_expand_tables_empty_text_preserves_marker() -> None:
    tables = {"tb_0001": {"text": ""}}
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


def test_full_mode_expands_table_into_context() -> None:
    ctx = _render([
        _chunk(text="요건 본문 [TABLE: tb_0001] 이후 문장",
               tables={"tb_0001": {"text": "| 항목 | 값 |\n| 온도 | 2200°F |"}}),
    ])
    assert "2200°F" in ctx
    assert "[TABLE:" not in ctx  # 마커가 표 내용으로 치환됨


def test_snippets_mode_also_expands_within_cap() -> None:
    # 타 variant 의 snippets 모드에서도 (캡 안의) 마커는 치환된다.
    ctx = _render([
        _chunk(snippet="요건 [TABLE: tb_0001] 끝",
               tables={"tb_0001": {"text": "표내용"}}),
    ], mode="snippets")
    assert "표내용" in ctx and "[TABLE:" not in ctx


def test_full_mode_no_tables_leaves_marker_visible() -> None:
    # tables 가 없으면 마커가 컨텍스트에 그대로 남아 누락이 가시화된다.
    ctx = _render([_chunk(text="요건 [TABLE: tb_0001] 끝", tables=None)])
    assert "[TABLE: tb_0001]" in ctx


# --- to_snapshot (full 모드 — 치환 전 원본 text + tables 보존) ---------------
def test_snapshot_full_mode_preserves_original_text_and_tables() -> None:
    b = ContextBuilder(capture_mode="full")
    c = _chunk(text="body [TABLE: tb_0001] tail",
               tables={"tb_0001": {"text": "TABLEDATA"}})
    pack = b.build(
        interaction_id="i", query_text="q", chat_history=(),
        conversation_summary=None, scenario_object="n_a", scenario_depth="n_a",
        entities={}, chunks=[c],
    )
    snap = b.to_snapshot(pack)
    # 스냅샷에는 치환 *전* 원본이 남아 로직 변경 시에도 재현 가능(원칙 5).
    assert snap["chunks"][0]["text"] == "body [TABLE: tb_0001] tail"
    assert snap["chunks"][0]["tables"] == {"tb_0001": {"text": "TABLEDATA"}}
