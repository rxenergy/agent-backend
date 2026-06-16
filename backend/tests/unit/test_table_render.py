from __future__ import annotations

from app.application.context.table_render import (
    html_table_to_markdown,
    table_body_markdown,
)


# --- html_table_to_markdown ------------------------------------------------
def test_html_table_with_header_row() -> None:
    html = ("<table><tr><th>항목</th><th>한계값</th></tr>"
            "<tr><td>PCT</td><td>2200°F</td></tr>"
            "<tr><td>산화</td><td>17%</td></tr></table>")
    md = html_table_to_markdown(html)
    assert md == (
        "| 항목 | 한계값 |\n"
        "| --- | --- |\n"
        "| PCT | 2200°F |\n"
        "| 산화 | 17% |"
    )


def test_html_table_first_row_as_header_when_no_th() -> None:
    html = "<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>"
    md = html_table_to_markdown(html)
    assert md == "| a | b |\n| --- | --- |\n| 1 | 2 |"


def test_html_table_inline_tags_flattened_and_br_to_space() -> None:
    html = ("<table><tr><td><b>P₁</b></td><td>line1<br>line2</td></tr></table>")
    md = html_table_to_markdown(html)
    # 인라인 태그는 텍스트로 평탄화, <br> 은 공백. 단일 행도 헤더+구분선(GFM 필수).
    assert md == "| P₁ | line1 line2 |\n| --- | --- |"


def test_html_table_pipe_escaped_in_cell() -> None:
    html = "<table><tr><td>a|b</td><td>c</td></tr></table>"
    md = html_table_to_markdown(html)
    assert md == "| a\\|b | c |\n| --- | --- |"


def test_html_table_ragged_rows_padded() -> None:
    # 셀 수가 다른 행은 최다 열 기준으로 빈칸 패딩.
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>1</td></tr></table>"
    md = html_table_to_markdown(html)
    assert md == "| a | b | c |\n| --- | --- | --- |\n| 1 |  |  |"


def test_html_table_none_when_no_table() -> None:
    assert html_table_to_markdown("just text") is None
    assert html_table_to_markdown("") is None
    assert html_table_to_markdown("<table></table>") is None  # 행 없음


# --- table_body_markdown (markdown 우선, html 변환, fallback) ----------------
def test_table_body_prefers_markdown() -> None:
    entry = {"markdown": "| a |\n| --- |", "html": "<table>...</table>"}
    assert table_body_markdown(entry) == "| a |\n| --- |"


def test_table_body_converts_html_when_no_markdown() -> None:
    entry = {"markdown": "", "html": "<table><tr><td>x</td></tr></table>"}
    assert table_body_markdown(entry) == "| x |\n| --- |"


def test_table_body_keeps_raw_html_when_unconvertible() -> None:
    # <table> 없는 html(변환 불가) → 원본 보존(데이터 손실 금지).
    entry = {"markdown": "", "html": "<div>not a table</div>"}
    assert table_body_markdown(entry) == "<div>not a table</div>"


def test_table_body_empty_when_no_content() -> None:
    assert table_body_markdown({"markdown": "", "html": ""}) == ""
    assert table_body_markdown({}) == ""
    assert table_body_markdown("not a dict") == ""
