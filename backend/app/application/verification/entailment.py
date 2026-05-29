from __future__ import annotations

import json
from dataclasses import dataclass

from app.domain.verification import Claim
from app.ports.llm import GrammarSpec, LLMPort

# v3.1 Node 15 step 3 — textual entailment. spec 옵션 B(PoC 기본): utility LLM
# 1 batch 호출 + schema-constrained. 4-step 중 *유일하게* "claim 이 근거에
# 충실한가"를 직접 답하는 단계(나머지 3개는 necessary-not-sufficient). NLI
# cross-encoder(옵션 A)는 후속.
#
# 미가용/파싱불가 시 verdicts 비움 → verifier 가 "entailment 미실행" 으로 처리
# (citation-grounded degrade, entailment_model=None 로 기록).

ENTAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["supported", "contradicted", "unsupported"]},
                    "score": {"type": "number"},
                },
                "required": ["claim_id", "status"],
            },
        }
    },
    "required": ["verdicts"],
}


@dataclass(frozen=True)
class EntailmentVerdict:
    status: str  # supported | contradicted | unsupported
    score: float | None = None


class EntailmentChecker:
    """배치 LLM 함의 판정. model_id 를 노출해 event 의 entailment_model 에 기록."""

    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    @property
    def model_id(self) -> str:
        return getattr(self._llm, "model_id", "unknown")

    async def check(
        self, claims: list[Claim], *, evidence_by_cite: dict[str, str]
    ) -> dict[str, EntailmentVerdict]:
        if not claims:
            return {}
        prompt = _build_prompt(claims, evidence_by_cite)
        try:
            res = await self._llm.generate(
                prompt,
                model_options={"temperature": 0.0},
                grammar=GrammarSpec(kind="json_schema", value=ENTAIL_SCHEMA),
            )
            return _parse(res.text)
        except Exception:  # noqa: BLE001
            return {}


def _build_prompt(claims: list[Claim], evidence_by_cite: dict[str, str]) -> str:
    lines = [
        "각 claim 이 제시된 근거에 의해 supported/contradicted/unsupported 인지 "
        "판정하라. 근거에 명시되지 않으면 unsupported, 근거와 모순되면 "
        "contradicted. JSON 만 출력.\n",
        "# 근거",
    ]
    for cid, text in evidence_by_cite.items():
        lines.append(f"[{cid}] {text}")
    lines.append("\n# claims")
    for c in claims:
        lines.append(f"- {c.id} (cite={c.cite_marker or '없음'}): {c.text}")
    return "\n".join(lines)


def _parse(text: str) -> dict[str, EntailmentVerdict]:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return {}
    data = json.loads(text[start : end + 1])
    out: dict[str, EntailmentVerdict] = {}
    for v in data.get("verdicts") or []:
        cid = v.get("claim_id")
        st = v.get("status")
        if not cid or st not in ("supported", "contradicted", "unsupported"):
            continue
        score = v.get("score")
        out[cid] = EntailmentVerdict(status=st, score=float(score) if score is not None else None)
    return out
