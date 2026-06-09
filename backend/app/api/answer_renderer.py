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

# 인용 마커 그룹 — 한 대괄호 안의 cite-N 하나 *또는* 결합형(쉼표/세미콜론/공백
# 구분). 모델이 계약을 어기고 `[cite-0, cite-2]` 처럼 묶어 내도 깨지지 않게 그룹째
# 매칭한 뒤 개별 cite-N 으로 분해한다(결정=코드 — [[model_over_rule]]).
_CITE_GROUP_RE = re.compile(r"\[\s*cite-\d+(?:[\s,;]+cite-\d+)*\s*\]")
_CITE_ID_RE = re.compile(r"cite-\d+")
# `format_citation` 출력: "[cite-0] [RG-1.206, Section C.I.4, p. 12, Rev. 5]".
# 앞의 [cite-N] 토큰을 떼고 바깥 대괄호 안의 사람용 라벨만 추출.
_FORMATTED_RE = re.compile(r"^\[cite-\d+\]\s*\[(.*)\]\s*$")
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
            return m.group(1).strip()
        return c.formatted.strip()
    return c.document_id or c.citation_id


def references_section(citations, renumber: dict[str, int]) -> str:
    """본문에서 참조된 인용만, 표시번호순. ADAMS URL 파생되면 마크다운 링크."""
    if not renumber:
        return ""
    by_id = {c.citation_id: c for c in citations}
    lines: list[str] = []
    for cid, num in sorted(renumber.items(), key=lambda kv: kv[1]):
        c = by_id.get(cid)
        if c is None:
            # 후보에 없는 참조(계약 위반 가능) — KeyError 대신 가시 표기.
            lines.append(f"{num}. (근거 메타 없음: {cid})")
            continue
        label = _citation_label(c)
        url = adams_url(c.document_id)
        lines.append(f"{num}. [{label}]({url})" if url else f"{num}. {label}")
    # 헤더와 리스트 사이 빈 줄 필수 — 일부 마크다운 파서(marked.js 등)는 빈 줄이
    # 없으면 리스트를 헤더 단락에 붙여 한 줄로 렌더한다.
    return "**근거 (References)**\n\n" + "\n".join(lines)


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
