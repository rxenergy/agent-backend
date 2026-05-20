from __future__ import annotations

from typing import Protocol

from app.domain.interaction import AgentRequest, AgentResponse


class AgentRunner(Protocol):
    """Internal strategy port. Selected at boot via `AGENT_VARIANT` env var;
    swapped without domain/API/other-variant changes (spec §25.2, memory:
    agent-backend-architecture)."""

    variant_id: str

    async def run(self, request: AgentRequest) -> AgentResponse: ...
