from __future__ import annotations

from enum import Enum


class RefusalReason(str, Enum):
    CLARIFICATION_REQUIRED = "clarification_required"
    RETRIEVAL_NO_RESULT = "retrieval_no_result"
    VERIFICATION_FAILED = "verification_failed"
    PARTIAL_ANSWER = "partial_answer"
    REFUSAL = "refusal"
    UNSUPPORTED_SCENARIO = "unsupported_scenario"
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
