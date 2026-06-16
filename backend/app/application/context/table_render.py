"""표(table) 렌더 — HTML 표를 GFM markdown 파이프표로 변환.

배경(spec_driven_table_citation_granularity 후속): 인덱싱 단계가 표를 `markdown` 또는
`html` 로 싣는데, 실데이터는 대부분 `html` 만 있다. OpenWebUI(marked.js)는 raw `<table>`
HTML 을 — 특히 SSE 스트리밍으로 토큰 경계가 쪼개지면 — 안정적으로 렌더하지 못하고 원문
텍스트를 그대로 노출하는 경우가 있다. 그래서 표 본문을 사용자에게 보일 때는 항상 **GFM
파이프표(markdown)** 로 정규화한다: `markdown` 이 있으면 그대로, 없고 `html` 만 있으면
이 모듈이 stdlib `html.parser` 로 파싱해 파이프표로 변환한다(외부 의존성 없음).

변환은 표 구조(행·셀)만 추출하는 보수적 변환이다 — 셀 안의 인라인 태그(`<b>`, `<sup>`)는
텍스트로 평탄화하고, rowspan/colspan 은 단순 셀로 취급한다(규제 표의 수치·기준 보존이 목적,
완벽한 레이아웃 재현이 아님). 파싱 실패 시 원본 html 문자열을 그대로 반환한다(데이터 손실
금지 — 깨진 변환보다 원문 보존)."""
from __future__ import annotations

from html.parser import HTMLParser


class _TableExtractor(HTMLParser):
    """첫 `<table>` 의 행(`<tr>`)·셀(`<td>`/`<th>`)을 2차원 텍스트로 추출.

    중첩 표는 바깥 표만 취한다(depth 추적). 셀 텍스트는 인라인 태그를 무시하고 데이터만
    모은다. `<br>` 는 공백으로(파이프표는 셀 내 줄바꿈 불가)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._depth = 0          # <table> 중첩 깊이
        self._in_cell = False
        self._cur_row: list[str] | None = None
        self._cur_cell: list[str] = []
        self._header_flags: list[bool] = []   # 행별 th-여부(첫 행 헤더 판정 보조)
        self._cur_is_header = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "table":
            self._depth += 1
            return
        if self._depth != 1:
            return  # 바깥 표 밖(또는 중첩 표 안)은 무시
        if tag == "tr":
            self._cur_row = []
            self._cur_is_header = False
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cur_cell = []
            if tag == "th":
                self._cur_is_header = True
        elif tag == "br" and self._in_cell:
            self._cur_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("td", "th"):
            self._in_cell = False
            text = " ".join("".join(self._cur_cell).split())
            if self._cur_row is not None:
                self._cur_row.append(text)
        elif tag == "tr":
            if self._cur_row:
                self.rows.append(self._cur_row)
                self._header_flags.append(self._cur_is_header)
            self._cur_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._depth == 1:
            self._cur_cell.append(data)


def _escape_cell(text: str) -> str:
    """파이프표 셀 — `|` 는 구분자라 escape, 개행은 공백으로(셀은 한 줄)."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def html_table_to_markdown(html: str) -> str | None:
    """HTML `<table>` → GFM 파이프표. 표가 없거나 행이 없으면 None.

    첫 행을 헤더로 쓴다(th 가 있으면 그 행, 없으면 첫 데이터 행). 열 수는 최다 행 기준으로
    맞추고 모자란 셀은 빈칸으로 채운다. 파싱 예외/빈 결과는 None → 호출부가 원본 html
    fallback."""
    if not html or "<table" not in html.lower():
        return None
    try:
        p = _TableExtractor()
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 — 깨진 HTML 은 변환 포기(원문 fallback)
        return None
    rows = [r for r in p.rows if r]
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    if ncols == 0:
        return None

    def _pad(r: list[str]) -> list[str]:
        return [_escape_cell(c) for c in r] + [""] * (ncols - len(r))

    header = _pad(rows[0])
    body = [_pad(r) for r in rows[1:]]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def table_body_markdown(entry: dict) -> str:
    """표 엔트리({tag,caption,markdown,html}) → 사용자 표시용 표 본문(GFM markdown).
    `markdown` 우선, 없으면 `html` 을 파이프표로 변환, 변환 실패 시 원본 html, 둘 다 없으면
    빈 문자열. References·CONTEXT 양쪽이 이 단일 진입점을 쓴다(렌더 일관)."""
    if not isinstance(entry, dict):
        return ""
    md = (entry.get("markdown") or "").strip()
    if md:
        return md
    html = (entry.get("html") or "").strip()
    if not html:
        return ""
    converted = html_table_to_markdown(html)
    return converted if converted else html
