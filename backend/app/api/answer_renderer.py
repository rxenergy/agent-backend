"""Compose the OpenWebUI-friendly answer body from a structured AgentResponse.

The workflow emits `answer_text` carrying inline `[cite-N]` markers (the Claim
Verifier contract — never changed) plus structured `citations` and safety axes
(`verification_status`, `regulatory_grounding`, `refusal_reason`). This module
is the *presentation boundary* (mirrors `thinking_renderer`): it owns how those
become clean markdown in the OpenAI-compatible `content` field — the only
citation surface every OpenAI-compatible client (OpenWebUI included) renders.

Transforms applied here (display-only; `[cite-N]` stays the verifier contract):
  1. inline `[cite-N]` → renumbered `[n]` (first-appearance), via one-shot
     `rewrite_inline` (non-streaming) or `CiteStreamRewriter` (streaming).
  2. a `**근거 (References)**` section listing only the citations actually
     referenced, renumbered, linked to an ADAMS PDF when derivable.
  3. safety caveats (부분 답변 / 규제 근거 미검증) as markdown blockquote
     callouts, composed from structured fields — NOT baked into `answer_text`
     (so streaming and non-streaming render identically; design ref
     docs/plans/answer_body_rendering.plan.v1.md, decision A).

Hard refusals (verification fail, insufficient evidence, …) render the refusal
message alone — no references, no caveats.
"""
from __future__ import annotations

import re

from app.application.context.citation_format import adams_url
from app.application.context.table_render import table_body_markdown

# 인용 마커 그룹 — 한 대괄호 안의 cite-N 하나 *또는* 결합형(쉼표/세미콜론/공백
# 구분). 모델이 계약을 어기고 `[cite-0, cite-2]` 처럼 묶어 내도 깨지지 않게 그룹째
# 매칭한 뒤 개별 cite-N 으로 분해한다(결정=코드 — [[model_over_rule]]).
_CITE_GROUP_RE = re.compile(r"\[\s*cite-\d+(?:[\s,;]+cite-\d+)*\s*\]")
_CITE_ID_RE = re.compile(r"cite-\d+")
# `format_citation` 출력: "[cite-0] [RG-1.206, Section C.I.4, p. 12, Rev. 5] (권고·비구속 지침)".
# 선행 [cite-N] 과 바깥 대괄호를 떼고 사람용 라벨(+ 권위 태그)만 추출한다.
# 주의: 권위 태그 " (…)" 가 뒤에 붙으므로 `$` 로 닫지 않고 꼬리(group 2)를 따로 받는다
# — 안 그러면 매칭 실패로 fallback 이 [cite-N] 접두를 그대로 남긴다(이 버그가 References
# 에 `[cite-4]` 가 새던 원인).
_FORMATTED_RE = re.compile(r"^\[cite-\d+\]\s*\[(.*)\]\s*(.*)$")
# fallback — 예상 밖 형식이라도 최소한 선행 [cite-N] 접두만 떼어낸다.
_CITE_PREFIX_RE = re.compile(r"^\s*\[cite-\d+\]\s*")
# 끝에 붙는 권위 태그 " (신청자 주장)" 등 — References 노출 차단용(꼬리 한정).
# 라벨 중간 괄호(`(preamble)`, `Rev. 3 (2017)`)는 끝이 아니라 보존된다.
_WEIGHT_TAG_RE = re.compile(r"\s*\([^()]*\)\s*$")
# 결합형 prefix 홀드백용 — `[` 뒤에 cite-그룹으로 *자랄 수 있는* 문자만(c/i/t/e,
# 숫자, `-`, 구분자, 공백). `]` 가 닫히거나 알파벳 밖 문자가 나오면 즉시 판정한다.
_GROUP_PREFIX_CHARS = frozenset("cite-0123456789,; \t\n\r")

# answer_text 에 baking 되지 않는 고지(boundary 단일 합성). refusal_reason 이
# None(정상) 또는 partial_answer(소프트)일 때만 본문에 trailer 를 단다.
_SOFT_OUTCOMES = (None, "partial_answer")


def renumber_map(text: str) -> dict[str, int]:
    """본문 등장 순서(첫 등장 기준)로 cite-id → 1-base 표시번호. 결합형
    `[cite-0, cite-2]` 은 그룹 내 등장 순서대로 각 cite-id 를 매긴다."""
    seen: dict[str, int] = {}
    for m in _CITE_GROUP_RE.finditer(text or ""):
        for cid in _CITE_ID_RE.findall(m.group(0)):
            if cid not in seen:
                seen[cid] = len(seen) + 1
    return seen


def rewrite_inline(text: str, renumber: dict[str, int]) -> str:
    """본문 인용 그룹 → 표시번호. 단건 `[cite-N]`→`[n]`, 결합형
    `[cite-0, cite-2]`→`[1][2]`(OpenWebUI 는 분리된 대괄호만 링크). 맵에 없는
    (계약 위반) cite-id 가 그룹에 섞이면 그룹 원형을 유지한다."""
    def _repl(m: re.Match) -> str:
        cids = _CITE_ID_RE.findall(m.group(0))
        if any(renumber.get(c) is None for c in cids):
            return m.group(0)
        return "".join(f"[{renumber[c]}]" for c in cids)
    return _CITE_GROUP_RE.sub(_repl, text or "")


def _citation_label(c) -> str:
    if c.formatted:
        m = _FORMATTED_RE.match(c.formatted)
        if m:
            # group 2(권위 태그 "(신청자 주장)" 등)는 References 에 노출하지 않는다 —
            # 권위 등급은 모델 보정용 CONTEXT 신호일 뿐, 사용자용 출처 라인엔 문서
            # 식별 정보(inner)만 남긴다. 태그는 본문 권위 서술로 이미 반영된다.
            return m.group(1).strip()
        # 예상 밖 형식 — 선행 [cite-N] 접두만 떼고, 뒤따르는 권위 태그도 제거.
        return _WEIGHT_TAG_RE.sub("", _CITE_PREFIX_RE.sub("", c.formatted)).strip()
    return c.document_id or c.citation_id


def _citation_url(c) -> str | None:
    """References 링크 URL. 우선순위(사용자 결정):
      1. 인덱스 원문 URL(source_url) — doc_metadata.Url(ADAMS) / download_pdfLink
         (govinfo·10CFR) / detailsLink. 인덱싱 시점 그 청크 원문 경로라 가장 정확.
      2. adams_url(document_id) — ML번호 정규식 재구성 fallback(구 시드 — source_url 부재).
      3. None → 호출측이 평문 라벨(무근거/404 링크 회피).

    page 딥링크는 URL 이 `.pdf` 로 끝나고 page 가 정수일 때만 `#page=N` 을 붙인다
    (HTML detailsLink·eCFR 류엔 PDF fragment 가 무의미). source_url 에 이미 fragment·
    query 가 있으면 page 앵커를 덧붙이지 않는다(원본 앵커 보존)."""
    url = getattr(c, "source_url", None) or adams_url(c.document_id)
    if not url:
        return None
    if (isinstance(c.page, int)
            and url.lower().endswith(".pdf")
            and "#" not in url):
        url = f"{url}#page={c.page}"
    return url


def _render_reference_tables(tables) -> str:
    """citation 의 tables(list[dict] — {tag,caption,markdown,html})를 References 표
    블록 마크다운으로. caption(있으면 `**bold**`) + markdown(우선) / html(차순). markdown·
    html 둘 다 비면 그 엔트리는 건너뛴다. 여러 엔트리는 빈 줄로 누적(pack._expand_tables
    의 누적 규칙과 동형). 표 본문이 하나도 없으면 빈 문자열(호출부가 표 블록 미삽입).

    marked.js 는 GFM 파이프표·raw `<table>` 모두 렌더하나, 파이프표는 **앞뒤 빈 줄로
    격리**돼야 단락에 병합되지 않는다 — 호출부(references_section)가 표 블록을 `\n\n` 로
    감싸 분리 단락으로 둔다."""
    if not tables:
        return ""
    blocks: list[str] = []
    for e in tables:
        if not isinstance(e, dict):
            continue
        # 표 본문은 GFM markdown 으로 정규화 — markdown 우선, html 만 있으면 파이프표로
        # 변환(OpenWebUI 가 raw HTML 을 텍스트로 노출하는 문제 회피, table_render).
        body = table_body_markdown(e).strip()
        if not body:
            continue
        caption = (e.get("caption") or "").strip()
        blocks.append(f"**{caption}**\n\n{body}" if caption else body)
    return "\n\n".join(blocks)


def references_section(citations, renumber: dict[str, int]) -> str:
    """본문에서 참조된 인용만, **본문 등장 순서(표시번호순)**로 나열한다. 각 줄은
    본문 마커와 동일한 `[N]` 형식으로 시작한다(`[cite-N]`·`N.` 아님 — 본문↔근거
    번호가 한눈에 매칭). ADAMS/govinfo URL 파생되면 라벨에 마크다운 링크.

    표 보유 citation(spec_driven_table_citation_references)은 라벨 줄 *아래* 에 실제
    표(markdown/HTML)를 빈 줄로 격리해 렌더한다 — 담당자가 답변 화면에서 표를 직접
    확인. 표 없는 citation 은 기존과 동일(라벨/링크만)."""
    if not renumber:
        return ""
    by_id = {c.citation_id: c for c in citations}
    # 단락(segment) 단위 조립 — 표 없는 라벨은 hard break("  \n")로 한 단락에 모으고,
    # 표 보유 항목은 빈 줄(`\n\n`)로 격리한 독립 단락으로 둔다(파이프표 단락 병합 방지).
    segments: list[str] = []
    pending_labels: list[str] = []  # 아직 단락에 안 묶인 표-없는 라벨 줄들.

    def _flush_labels() -> None:
        if pending_labels:
            segments.append("  \n".join(pending_labels))
            pending_labels.clear()

    # renumber 값(표시번호)은 본문 첫 등장 순으로 부여되므로 그 순으로 정렬하면
    # 곧 본문 출력 순서다.
    for cid, num in sorted(renumber.items(), key=lambda kv: kv[1]):
        c = by_id.get(cid)
        if c is None:
            # 후보에 없는 참조(계약 위반 가능) — KeyError 대신 가시 표기.
            pending_labels.append(f"[{num}] (근거 메타 없음: {cid})")
            continue
        label = _citation_label(c)
        url = _citation_url(c)
        # 이중 링크 방지 — format_citation 이 ADAMS 문서 inner 에 이미 `[ML..](url)`
        # 마크다운 링크를 넣는 경우가 있다(_doc_link). 그 라벨을 다시 `[label](url)` 로
        # 감싸면 `[[ML..](url), …](url)` 중첩이 된다. 라벨에 이미 markdown 링크(`](`)가
        # 있으면 재감싸지 않고 평문으로 둔다(라벨 안 링크가 출처로 동작).
        if url and "](" not in label:
            label_line = f"[{num}] [{label}]({url})"
        else:
            label_line = f"[{num}] {label}"
        # 표 cite(kind="table")만 표 블록을 렌더한다(spec_driven_table_citation_granularity).
        # chunk cite 는 tables=None 이라 라벨만 — 본문 근거와 표 근거가 분리되어, 본문만
        # 인용된 chunk 의 무관한 표가 노출되지 않는다(선행 "chunk 표 전량 렌더" 폐기).
        is_table = getattr(c, "kind", "chunk") == "table"
        table_md = _render_reference_tables(getattr(c, "tables", None)) if is_table else ""
        if table_md:
            # 표 보유 항목 — 직전까지 모인 라벨 단락을 닫고, 라벨+표를 빈 줄로 격리한
            # 독립 단락으로 추가(라벨 ↔ 표 사이도 빈 줄 — marked.js 파이프표 격리).
            _flush_labels()
            segments.append(f"{label_line}\n\n{table_md}")
        else:
            pending_labels.append(label_line)
    _flush_labels()
    # 헤더와 본문 사이 빈 줄 필수(marked.js 단락 병합 방지). 단락 간 빈 줄(`\n\n`)로
    # 잇는다 — 표 단락이 인접 라벨과 병합되지 않게.
    return "**근거 (References)**\n\n" + "\n\n".join(segments)


def caveat_callouts(response) -> str:
    """부분/규제 미검증 고지 → 마크다운 blockquote callout. 구조화 필드에서 합성."""
    blocks: list[str] = []
    vs = (response.verification_status or "").upper()
    if vs == "PARTIAL" or response.refusal_reason == "partial_answer":
        blocks.append(
            "> ⚠️ **부분 답변** — 일부 주장의 근거·인용이 검증 임계값을 충족하지 못했습니다."
        )
    if getattr(response, "regulatory_grounding", "n_a") == "unverified":
        blocks.append(
            "> ⚠️ **규제 근거 미검증** — 현재 인덱스에 조문 ID·발효일·권위 등급 메타가"
            " 없어 규제 차원 검증은 수행되지 않았습니다(인용 충실성만 검증)."
        )
    return "\n\n".join(blocks)


def answer_trailer(response, renumber: dict[str, int]) -> str:
    """본문 *뒤* 에 붙는 부분(callout + References). hard refusal 이면 빈 문자열."""
    if response.refusal_reason not in _SOFT_OUTCOMES:
        return ""
    parts: list[str] = []
    callouts = caveat_callouts(response)
    if callouts:
        parts.append(callouts)
    refs = references_section(response.citations, renumber)
    if refs:
        parts.append("---\n\n" + refs)  # HR 뒤 빈 줄 — setext-heading 오해석 방지.
    return "\n\n".join(parts)


def compose_answer_body(response) -> str:
    """비스트리밍/단발 경로용 — 본문 마커 재번호 + trailer 합성한 완성 content.
    스트리밍은 `CiteStreamRewriter` + `answer_trailer` 로 동일 결과를 점진 생성."""
    body = response.answer_text or ""
    if response.refusal_reason not in _SOFT_OUTCOMES:
        return body  # hard refusal: 거부 메시지만.
    renumber = renumber_map(body)
    display = rewrite_inline(body, renumber)
    trailer = answer_trailer(response, renumber)
    return display + (("\n\n" + trailer) if trailer else "")


def _is_group_prefix(buf: str) -> bool:
    """`buf`(반드시 `[` 로 시작)가 아직 인용 그룹으로 *자랄 수 있는* 부분열인가.
    `]` 가 아직 없고 `[` 뒤 모든 문자가 cite-그룹 알파벳이면 홀드백한다 —
    결합형 `[cite-0, cite-2]` 가 토큰 경계(`[cite-0,` | ` cite-2]`)로 쪼개져
    들어와도 닫힘 `]` 전에 raw 로 새지 않게 한다(advisor — 매칭 정규식만 넓히면
    스트리밍에선 무효)."""
    if "]" in buf:
        return False  # 닫혔는데 그룹 매칭 실패 → 더 기다려도 안 됨.
    return all(ch in _GROUP_PREFIX_CHARS for ch in buf[1:])


class CiteStreamRewriter:
    """스트리밍 토큰의 인용 그룹 → 표시번호 치환기. 단건 `[cite-N]`→`[n]`,
    결합형 `[cite-0, cite-2]`→`[1][2]`. 토큰 경계를 가로지르는 부분열을 버퍼링하고
    first-appearance 로 `renumber` 맵을 증분 구축한다(종료 후 trailer 의 References
    가 동일 번호 사용). 그룹으로 자랄 수 없는 `[` 는 즉시 통과시킨다(`[1]`, `[item`
    등은 과홀드하지 않음)."""

    def __init__(self) -> None:
        self.renumber: dict[str, int] = {}
        self._buf = ""

    def _render_group(self, group_text: str) -> str:
        parts: list[str] = []
        for cid in _CITE_ID_RE.findall(group_text):
            if cid not in self.renumber:
                self.renumber[cid] = len(self.renumber) + 1
            parts.append(f"[{self.renumber[cid]}]")
        return "".join(parts)

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            i = self._buf.find("[")
            if i == -1:
                out.append(self._buf)
                self._buf = ""
                break
            if i > 0:
                out.append(self._buf[:i])
                self._buf = self._buf[i:]
            m = _CITE_GROUP_RE.match(self._buf)  # 완성된 [cite-N(, cite-M)*] ?
            if m:
                out.append(self._render_group(m.group(0)))
                self._buf = self._buf[m.end():]
                continue
            if _is_group_prefix(self._buf):
                break  # 아직 인용 그룹으로 자랄 수 있음 → 더 받는다.
            # 확정적으로 cite 그룹 아님('[1]', '[item' 등) → '[' 방출 후 계속.
            out.append("[")
            self._buf = self._buf[1:]
        return "".join(out)

    def flush(self) -> str:
        """스트림 종료 — 남은 버퍼(불완전 토큰 포함) 방출."""
        rest = self._buf
        self._buf = ""
        return rest
