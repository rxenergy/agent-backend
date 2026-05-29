from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.application.retrieval.snippet import regex_sentence_split
from app.domain.verification import Claim, ClaimType
from app.ports.llm import GrammarSpec, LLMPort

# v3.1 Node 14 — claim_decompose. 답변을 atomic claim 리스트로 분해.
# 1차: utility LLM + JSON-schema grammar(temperature 0). 실패(파싱불가/미가용)
# 시: 결정론 fallback(문장 분할 + [cite-N] 추출). 어느 경로였는지 `method` 로
# 기록 — silent degrade 금지(advisor).

import re

_CITE = re.compile(r"\[(cite-\d+)\]")

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "cite_marker": {"type": "string"},
                    "claim_type": {"type": "string"},
                },
                "required": ["id", "text"],
            },
        }
    },
    "required": ["claims"],
}

_PROMPT = (
    "다음 답변을 검증 가능한 atomic 사실 주장(claim) 목록으로 분해하라. "
    "각 claim 은 단일 인용으로 검증 가능한 최소 단위이며, 답변에 포함된 "
    "[cite-N] 마커가 있으면 cite_marker 로 싣는다. JSON 만 출력.\n\n답변:\n{answer}"
)


@dataclass(frozen=True)
class DecomposeResult:
    claims: tuple[Claim, ...]
    method: str  # "llm" | "fallback"
    prompt_hash: str | None


class ClaimDecomposer:
    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    async def decompose(self, answer_text: str) -> DecomposeResult:
        prompt = _PROMPT.format(answer=answer_text)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        try:
            res = await self._llm.generate(
                prompt,
                model_options={"temperature": 0.0},
                grammar=GrammarSpec(kind="json_schema", value=CLAIM_SCHEMA),
            )
            claims = _parse_llm_claims(res.text)
            if claims:
                return DecomposeResult(tuple(claims), "llm", prompt_hash)
        except Exception:  # noqa: BLE001 — 미가용/파싱불가 → 결정론 fallback
            pass
        return DecomposeResult(
            tuple(_fallback_decompose(answer_text)), "fallback", prompt_hash
        )


def _parse_llm_claims(text: str) -> list[Claim]:
    text = text.strip()
    # grammar 미적용 백엔드가 코드펜스를 붙일 수 있어 관대하게 추출.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return []
    data = json.loads(text[start : end + 1])
    out: list[Claim] = []
    for i, c in enumerate(data.get("claims") or []):
        t = (c.get("text") or "").strip()
        if not t:
            continue
        out.append(
            Claim(
                id=str(c.get("id") or f"cl-{i}"),
                text=t,
                cite_marker=c.get("cite_marker") or None,
                claim_type=str(c.get("claim_type") or ClaimType.OTHER.value),
            )
        )
    return out


def _fallback_decompose(answer_text: str) -> list[Claim]:
    """결정론 분해 — 문장 단위, 문장 내 첫 [cite-N] 를 cite_marker 로. cite 가
    없는 문장도 claim 으로 남겨 verify 가 unsupported 로 판정하게 한다(누락 은폐 X)."""
    claims: list[Claim] = []
    for i, sent in enumerate(regex_sentence_split(answer_text)):
        m = _CITE.search(sent)
        claims.append(
            Claim(id=f"cl-{i}", text=sent, cite_marker=m.group(1) if m else None)
        )
    return claims
