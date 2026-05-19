from __future__ import annotations

from typing import Any, Protocol

from app.domain.interaction import InteractionEvent


class EventSinkPort(Protocol):
    async def write_interaction_event(self, event: InteractionEvent) -> None: ...

    async def write_context_snapshot(self, interaction_id: str, payload: dict[str, Any]) -> None: ...

    async def write_prompt_render_record(
        self, interaction_id: str, payload: dict[str, Any]
    ) -> None: ...

    async def write_tool_result_record(
        self, interaction_id: str, payload: dict[str, Any]
    ) -> None: ...
