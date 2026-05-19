from __future__ import annotations

from typing import Protocol

from app.domain.interaction import AgentRequest, AgentResponse


class AgentRunner(Protocol):
    variant_id: str

    async def run(self, request: AgentRequest) -> AgentResponse: ...
