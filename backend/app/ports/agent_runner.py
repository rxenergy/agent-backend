from __future__ import annotations

from typing import AsyncIterator, Protocol

from app.application.agents.events import AgentEvent
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest, AgentResponse


class AgentRunner(Protocol):
    """Internal strategy port. Selected at boot via `AGENT_VARIANT` env var;
    swapped without domain/API/other-variant changes (spec §25.2).

    Capability metadata lives in `spec` (ADR-0008): variant_id, compatible_llms,
    required_tools, capability_tags — all loaded from `variants/registry.yaml`
    by `VariantSpecRegistry` (ADR-0006). Callers MUST read `runner.spec.*`
    rather than reflecting on class attributes.

    Variants expose two execution surfaces:
      • `run()` — single-shot; returns the final response.
      • `run_stream()` — async iterator yielding intermediate progress
        events (step / tool / token / reasoning) followed by one `final`
        event carrying the `AgentResponse`. The SSE layer consumes this.
    """

    spec: VariantSpec

    async def run(self, request: AgentRequest) -> AgentResponse: ...

    def run_stream(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentEvent]: ...
