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
