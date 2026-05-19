from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ToolStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ToolErrorCode(str, Enum):
    TIMEOUT = "tool_timeout"
    UNAVAILABLE = "tool_unavailable"
    INVALID_INPUT = "tool_invalid_input"
    EMPTY_RESULT = "tool_empty_result"
    PERMISSION_DENIED = "tool_permission_denied"
    SCHEMA_MISMATCH = "tool_schema_mismatch"
    INTERNAL_ERROR = "tool_internal_error"


class ToolResult(BaseModel):
    """v2 §8.4 — frozen Tool invocation result."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    tool_version: str
    status: Literal["success", "partial", "failed"]
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int
    input_hash: str
    output_hash: str | None = None
    trace_id: str
    retry_count: int = 0
