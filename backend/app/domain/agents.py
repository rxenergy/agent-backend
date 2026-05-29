from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VariantSpec(BaseModel):
    """Self-describing capability metadata for an AgentRunner variant.

    Loaded from `variants/registry.yaml` (ADR-0006). Replaces the scattered
    class attributes (`variant_id`, `compatible_llms`) on runner classes so
    `/v1/models`, the OpenAI-compatible split, and runner↔llm compatibility
    checks read from a single typed source (ADR-0008).
    """

    model_config = ConfigDict(frozen=True)

    variant_id: str
    compatible_llms: tuple[str, ...] = ("*",)
    """`("*",)` means pool-wide compatibility (all configured llms). Otherwise an
    explicit allow-list of llm pool ids."""

    required_tools: tuple[str, ...] = ()
    """Tool names (matching `tools/registry.yaml`) the runner expects to be wired."""

    required_scenarios: tuple[str, ...] = ()
    """Scenario object codes (e.g. O1..O4) the runner is intended to serve."""

    capability_tags: tuple[str, ...] = ()
    """Free-form tags surfaced in telemetry & UI (e.g. `retrieval`, `verification`)."""

    extras: dict[str, Any] = Field(default_factory=dict)
    """Reserved for future capability fields without breaking the schema."""

    def accepts_llm(self, llm_id: str) -> bool:
        if not self.compatible_llms or "*" in self.compatible_llms:
            return True
        return llm_id in self.compatible_llms


@dataclass(frozen=True)
class Budget:
    """v3.1 (hierarchical_corrective) LLM-call budget (spec §2.3, Appendix B
    `budgets`). The conductor increments `llm_calls_used` per LLM call and
    refuses with `RefusalReason.BUDGET_EXCEEDED` once the cap is hit. Frozen
    dataclass so it serializes into `InteractionEvent.budget` via asdict; the
    conductor tracks the live count separately and snapshots a Budget at the
    end. `budget_hit` records which node(s) tried to exceed the cap."""

    llm_calls_used: int = 0
    total_llm_call_budget: int = 8
    budget_hit: tuple[str, ...] = field(default_factory=tuple)
