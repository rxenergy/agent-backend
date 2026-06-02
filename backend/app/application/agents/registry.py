from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from app.domain.agents import VariantSpec
from app.ports.agent_runner import AgentRunner

if TYPE_CHECKING:
    from app.application.agents.llm_router import LLMRouter
    from app.application.context.pack import ContextBuilder
    from app.application.events.recorder import EventRecorder
    from app.application.prompting.renderer import PromptRenderer
    from app.application.prompting.resolver import PromptResolver
    from app.application.tool_runtime.executor import ToolExecutor
    from app.application.memory.summarizer import ConversationSummarizer
    from app.ports.event_sink import EventSinkPort


@dataclass
class AgentDeps:
    """Heterogeneous dependency bundle handed to every variant factory.

    Each variant picks only the fields it needs. Fields that are expensive to
    construct (postgres pool, tool executor, prompt resolver, classifier) are
    typed as `Any | None` so the bundle stays the same shape across profiles
    without forcing eager construction in the simple-variant case.
    """

    recorder: "EventRecorder"
    event_sink: "EventSinkPort"
    app_profile: str
    # Heavy-tail deps (built only when at least one enabled variant needs them).
    llm_router: "LLMRouter | None" = None
    utility_llm: Any = None
    tool_executor: "ToolExecutor | None" = None
    prompt_resolver: "PromptResolver | None" = None
    prompt_renderer: "PromptRenderer | None" = None
    context_builder: "ContextBuilder | None" = None
    classifier: Any = None
    summarizer: "ConversationSummarizer | None" = None
    # v3.1 (hierarchical_corrective) Node 4 룰 기반 plan 선택기. None 이면
    # 변형이 단일 hybrid 폴백(RetrievalPlanner.default)을 쓴다.
    retrieval_planner: Any = None
    # v3.1 Node 6 5-신호 evaluator. None 이면 변형이 RetrievalEvaluator.default().
    retrieval_evaluator: Any = None
    # v3.1 Node 7 결정론 recover. None 이면 변형이 RetrievalRecoverer.default().
    retrieval_recoverer: Any = None
    # v3.1 Layer 1 범위 한정(corpus_map). None 이면 변형이 CorpusMap.default()
    # (scope off, noise floor 0) 폴백.
    corpus_map: Any = None
    # Pass-through tunables — runners read what they care about.
    tunables: dict[str, Any] = field(default_factory=dict)


VariantFactory = Callable[[VariantSpec, AgentDeps], AgentRunner]


class VariantRegistry:
    """Self-registration table for AgentRunner variants (ADR-0004).

    Each variant module calls `@register_variant("<variant_id>")` on its
    factory function at import time. Discovery is triggered by importing
    `app.application.agents` (which in turn imports every variant module),
    so `VariantRegistry.known()` reflects whatever is on the Python path.

    `profiles.py` no longer hard-codes `KNOWN_VARIANTS` or per-variant
    if-blocks — adding a variant = new module + `variants/registry.yaml`
    entry + ADR.
    """

    _factories: dict[str, VariantFactory] = {}

    @classmethod
    def register(cls, variant_id: str, factory: VariantFactory) -> None:
        if variant_id in cls._factories and cls._factories[variant_id] is not factory:
            raise ValueError(
                f"VariantRegistry: duplicate variant_id {variant_id!r} "
                f"(existing={cls._factories[variant_id].__module__})"
            )
        cls._factories[variant_id] = factory

    @classmethod
    def known(cls) -> frozenset[str]:
        return frozenset(cls._factories.keys())

    @classmethod
    def get(cls, variant_id: str) -> VariantFactory:
        try:
            return cls._factories[variant_id]
        except KeyError as e:
            raise KeyError(
                f"variant {variant_id!r} not registered; known={sorted(cls._factories)}"
            ) from e

    @classmethod
    def build(
        cls, variant_id: str, spec: VariantSpec, deps: AgentDeps
    ) -> AgentRunner:
        return cls.get(variant_id)(spec, deps)


def register_variant(variant_id: str) -> Callable[[VariantFactory], VariantFactory]:
    """Decorator: register the wrapped factory under `variant_id`."""

    def _deco(fn: VariantFactory) -> VariantFactory:
        VariantRegistry.register(variant_id, fn)
        return fn

    return _deco
