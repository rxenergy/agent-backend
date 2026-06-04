from __future__ import annotations

from enum import Enum


class RefusalReason(str, Enum):
    CLARIFICATION_REQUIRED = "clarification_required"
    RETRIEVAL_NO_RESULT = "retrieval_no_result"
    VERIFICATION_FAILED = "verification_failed"
    PARTIAL_ANSWER = "partial_answer"
    REFUSAL = "refusal"
    UNSUPPORTED_SCENARIO = "unsupported_scenario"
    UNKNOWN_SCENARIO = "unknown_scenario"
    DATA_LIMITATION = "data_limitation"
    LLM_UNAVAILABLE = "llm_unavailable"
    # v3.1 (hierarchical_corrective)
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # Node 7 recover exhausted (WEAK gate)
    BUDGET_EXCEEDED = "budget_exceeded"  # LLM-call budget cap hit (spec §2.3)
    # scope/open-world (taxonomy plan D-4) — off-topic·out-of-role(권위 참칭·날조·
    # 원거리 도메인). scope_tier=T4 의 과이탈 분기가 이 사유로 정중 거부한다.
    OUT_OF_SCOPE = "out_of_scope"


class VerificationStatus(str, Enum):
    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    SKIPPED = "skipped"


class RetrievalError(Exception):
    """Base for retriever/document tool failures crossing the adapter boundary."""


class RetrievalTimeoutError(RetrievalError):
    """Retrieval call exceeded its timeout."""


class RetrievalUnavailableError(RetrievalError):
    """Retrieval backend unreachable or returned a 5xx response."""


class PromptProfileNotFoundError(Exception):
    """Raised when no prompt profile is registered for an (O, D) pair.

    The agent runner translates this into a first-class refusal
    (`RefusalReason.UNKNOWN_SCENARIO`) — never a silent fallback prompt.
    """

    def __init__(self, *, scenario_object: str, scenario_depth: str) -> None:
        self.scenario_object = scenario_object
        self.scenario_depth = scenario_depth
        super().__init__(
            f"no prompt profile registered for ({scenario_object}, {scenario_depth})"
        )
