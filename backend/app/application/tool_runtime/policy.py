from __future__ import annotations

from typing import Literal

from app.domain.tools import ToolResult


Decision = Literal["proceed", "fallback", "refuse"]


def decide_after_result(result: ToolResult, required: bool) -> Decision:
    """Map tool outcome to workflow next step.

    Required tool failure is enforced upstream by ToolExecutor (raises). This
    helper handles optional tool outcomes.
    """
    if result.status == "success":
        return "proceed"
    if result.status == "partial":
        return "proceed"
    return "refuse" if required else "fallback"
