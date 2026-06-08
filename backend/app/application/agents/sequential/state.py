from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.application.prompting.renderer import RenderedPrompt
from app.domain.classification import ClassificationResult
from app.domain.interaction import (
    AgentRequest,
    AgentResponse,
    Citation,
    ToolCallRecord,
)
from app.domain.memory import MemoryRef
from app.domain.retrieval import RetrieverSearchOutput
from app.ports.tool import ToolExecutionContext


@dataclass
class RunState:
    """Mutable workflow state passed between sequential nodes (ADR-0003).

    Each node reads what it needs and writes only its own outputs. A
    conductor instantiates `RunState` once per turn and passes it through
    its node pipeline.

    Frozen=False here is a deliberate compromise: the conductor accumulates
    intermediate values (tool_calls, citations, scores, hashes) step by step,
    and forcing `replace(state, ...)` at every step would balloon the
    conductor without providing observable benefit. Hash equivalence with
    the pre-refactor monolith is the load-bearing invariant; mutation
    discipline is a follow-up if needed.
    """

    request: AgentRequest
    started: float
    llm_id: str = ""

    # Node 1 outputs
    classification: ClassificationResult | None = None
    scenario_object: str = ""
    scenario_depth: str = ""

    # Tool call telemetry (Node 2+ accumulator)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_result_refs: list[str] = field(default_factory=list)
    ctx: ToolExecutionContext | None = None

    # Node 2 — memory / retrieval
    session_memory_injected: bool = False
    session_memory_ids: list[str] = field(default_factory=list)
    approved_memory_refs: list[MemoryRef] = field(default_factory=list)
    retrieval: RetrieverSearchOutput | None = None
    citations: tuple[Citation, ...] = ()

    # Node 3 — context + prompt + generation
    context_pack: Any = None  # ContextPack — Any to avoid circular import
    rendered_prompt: RenderedPrompt | None = None
    llm_text: str = ""
    llm_token_usage: dict[str, int] = field(default_factory=dict)
    llm_model_id: str = ""

    # Node 4 — verification
    verification_status: str = ""
    citation_completeness: float = 0.0
    faithfulness: float = 0.0

    # Node 5+ — assembled response
    response: AgentResponse | None = None
