from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.verification.entailment import EntailmentChecker, EntailmentVerdict
from app.domain.errors import VerificationStatus
from app.domain.verification import (
    Claim,
    ClaimChecks,
    ClaimStatus,
    ClaimVerification,
)

# v3.1 Node 15 — claim_verify. claim 별 4-step 회로(spec §7.2):
#   1. citation resolves?   — 결정론 set lookup
#   2. version match?       — 결정론 날짜(여기선 N/A 입력 → None; v1 항상 None)
#   3. textual entailment?  — 모델(EntailmentChecker). 미실행 시 None.
#   4. regulation_id syntax — 결정론 regex(claim 에 reg ID 있을 때만)
#
# verification_status 는 *claim 집계*에서 파생(구 _run_checks 2-scalar 대체):
#   contradicted 1개라도 → 답변 폐기(refuse) 신호.
#   전부 supported → PASS. 그 외(partial/unsupported 존재, contradicted 없음) → PARTIAL.
#
# entailment 미실행(None) 인 claim 은 citation-grounded 로 degrade(supported) 하되
# `entailment_ran=False` 로 기록 — entailment 가 유일한 충분조건이므로 "검증된
# supported" 와 구분돼야 한다.

_REG_ID = re.compile(r"\b(?:RG[-_ ]?\d|10\s?CFR|KINS[-_]|DSRS|SRP)\b", re.IGNORECASE)


@dataclass(frozen=True)
class VerifyResult:
    claims: tuple[ClaimVerification, ...]
    status: str  # VerificationStatus value
    contradicted: bool
    entailment_ran: bool


class ClaimVerifier:
    def __init__(self, entailment: EntailmentChecker) -> None:
        self._entailment = entailment

    async def verify(
        self,
        claims: list[Claim],
        *,
        resolvable_citation_ids: set[str],
        candidate_citation_ids: set[str],
        evidence_by_cite: dict[str, str],
        version_constraint: str | None = None,
        revision_by_cite: dict[str, str] | None = None,
    ) -> VerifyResult:
        if not claims:
            # 분해 결과가 비면 검증 불가 → 보수적으로 PARTIAL(완전 PASS 아님).
            return VerifyResult((), VerificationStatus.PARTIAL.value, False, False)

        verdicts = await self._entailment.check(claims, evidence_by_cite=evidence_by_cite)
        entailment_ran = bool(verdicts)
        revision_by_cite = revision_by_cite or {}

        out: list[ClaimVerification] = []
        n_supported = n_bad = 0
        contradicted = False
        for c in claims:
            cid = c.cite_marker
            citation_resolves = bool(cid) and (
                cid in resolvable_citation_ids or cid in candidate_citation_ids
            )
            # version match — 입력(constraint+revision) 둘 다 있을 때만, 아니면 N/A.
            version_match: bool | None = None
            rev = revision_by_cite.get(cid) if cid else None
            if version_constraint and rev:
                version_match = rev >= version_constraint
            regex_ok: bool | None = None
            if _REG_ID.search(c.text):
                regex_ok = True  # 형식상 reg ID 패턴 존재(정밀 검증은 후속)
            ent: EntailmentVerdict | None = verdicts.get(c.id)
            ent_score = ent.score if ent else None

            status = self._status_for(citation_resolves, version_match, ent)
            if status == ClaimStatus.SUPPORTED.value:
                n_supported += 1
            elif status == ClaimStatus.CONTRADICTED.value:
                contradicted = True
                n_bad += 1
            else:
                n_bad += 1

            out.append(
                ClaimVerification(
                    claim_id=c.id,
                    text=c.text,
                    status=status,
                    cite_marker=cid,
                    evidence_strip_ids=(cid,) if cid else (),
                    checks=ClaimChecks(
                        citation_resolves=citation_resolves,
                        version_match=version_match,
                        entailment_score=ent_score,
                        regulation_id_syntax_ok=regex_ok,
                    ),
                )
            )

        if contradicted:
            status_agg = VerificationStatus.FAIL.value
        elif n_bad == 0:
            status_agg = VerificationStatus.PASS.value
        else:
            status_agg = VerificationStatus.PARTIAL.value
        return VerifyResult(tuple(out), status_agg, contradicted, entailment_ran)

    @staticmethod
    def _status_for(
        citation_resolves: bool, version_match: bool | None, ent: EntailmentVerdict | None
    ) -> str:
        # version 충돌 확정 → contradicted(강한 음성).
        if version_match is False:
            return ClaimStatus.CONTRADICTED.value
        if not citation_resolves:
            return ClaimStatus.UNSUPPORTED.value
        if ent is not None:
            if ent.status == "contradicted":
                return ClaimStatus.CONTRADICTED.value
            if ent.status == "supported":
                return ClaimStatus.SUPPORTED.value
            return ClaimStatus.UNSUPPORTED.value  # entailment 가 unsupported
        # entailment 미실행 → citation-grounded degrade(supported, 단 entailment_ran=False 로 표면화).
        return ClaimStatus.SUPPORTED.value
