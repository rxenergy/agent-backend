from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# v3.1 (hierarchical_corrective) — Phase D Verify & Correct domain models.
#
# Frozen dataclasses (not pydantic): `ClaimVerification` is embedded into
# `InteractionEvent.claims` and `AgentResponse.claims` and serialized via
# `dataclasses.asdict()`. Enums are `(str, Enum)` like `VerificationStatus`
# so asdict-then-json yields the value ("supported") rather than the repr
# ("ClaimStatus.SUPPORTED").


class ClaimType(str, Enum):
    DEFINITION = "definition"
    REQUIREMENT = "requirement"
    VALUE = "value"
    PROCEDURE = "procedure"
    COMPARISON = "comparison"
    OTHER = "other"


class ClaimStatus(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True)
class Claim:
    """Node 14 output — an atomic factual claim decomposed from the answer.

    `cite_marker` is the citation id the claim carries (e.g. "cite-3"), or
    None when the LLM produced an uncited assertion (which the verifier will
    then mark unsupported)."""

    id: str
    text: str
    cite_marker: str | None = None
    claim_type: str = ClaimType.OTHER.value


@dataclass(frozen=True)
class ClaimChecks:
    """Node 15 — raw results of the 4-step verification circuit (spec §7.2).

    `version_match` / `regulation_id_syntax_ok` are None when not applicable
    (claim carries no date / no regulation id). `entailment_score` is None
    when the entailment step was skipped."""

    citation_resolves: bool = False
    version_match: bool | None = None
    entailment_score: float | None = None
    regulation_id_syntax_ok: bool | None = None


@dataclass(frozen=True)
class ClaimVerification:
    """Node 15 per-claim verdict. `status` is a `ClaimStatus` value."""

    claim_id: str
    text: str
    status: str
    cite_marker: str | None = None
    evidence_strip_ids: tuple[str, ...] = ()
    checks: ClaimChecks = field(default_factory=ClaimChecks)
