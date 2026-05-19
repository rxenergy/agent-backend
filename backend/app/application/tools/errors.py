from __future__ import annotations

from app.domain.tools import ToolErrorCode


class ToolFailure(Exception):
    def __init__(self, tool_name: str, code: ToolErrorCode, message: str = "") -> None:
        super().__init__(f"{tool_name}: {code.value}: {message}")
        self.tool_name = tool_name
        self.code = code
        self.message = message


class RequiredToolFailed(ToolFailure):
    """Raised when a required tool fails — workflow must refuse."""


class ToolTimeout(ToolFailure):
    def __init__(self, tool_name: str, timeout_ms: int) -> None:
        super().__init__(tool_name, ToolErrorCode.TIMEOUT, f"{timeout_ms}ms")
        self.timeout_ms = timeout_ms


class ToolUnknown(Exception):
    """Tool is not declared in registry — workflow bug."""
