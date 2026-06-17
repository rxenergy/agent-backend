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
    # Node 1 분류 프롬프트 source(registry 호스팅). v3.1 은 settings.classifier_backend
    # 와 무관하게 이 source 로 LLM 분류기를 강제 구성한다(variant 격리, 원칙 1).
    # None 이면 변형이 deps.classifier(settings 기반) 폴백.
    classification_prompt_source: Any = None
    # spec_driven_v1 — 검색 앞단 두 모델 노드 + 생성 프롬프트 source(registry 호스팅, sha
    # 핀). N1 Define Spec / N2 Query Formulation / N4 Generation. None 이면 변형이 run()
    # 에서 부트 배선 오류로 처리(프롬프트는 코드 인라인 금지).
    spec_driven_answer_spec_source: Any = None
    spec_driven_query_source: Any = None
    spec_driven_generation_source: Any = None
    # spec_driven_v1 N0 Triage(라우팅 판정) + N4-G General Generation(RAG 비대상 도메인
    # 질의 직답) 프롬프트 source(registry 호스팅, sha 핀). 설계:
    # docs/plans/spec_driven_general_query_routing.design.v1.md. None 이면 변형이 run()
    # 에서 부트 배선 오류로 처리(프롬프트는 코드 인라인 금지).
    spec_driven_triage_source: Any = None
    spec_driven_general_source: Any = None
    # composer variant N4 슬롯 파이프라인 프롬프트 source(registry 호스팅, sha 핀). 슬롯
    # 생성 / 종합(정리+다음액션) / L1 검수. None 이면 composer 가 계승한 generation_source
    # (단일 N4)로 graceful fallback(프롬프트 점진 도입). 설계:
    # docs/plans/spec_driven_slotwise_generation.design.v1.md.
    composer_slot_source: Any = None
    composer_synthesize_source: Any = None
    composer_slot_verify_source: Any = None
    # composer v2 — 책임 재분배(answer_spec_query_responsibility_split.design.v1): N1 답변
    # 설계(검색지식 제거)·N2 검색설계(address map 흡수)·슬롯 role 소비. composer 만 쓴다
    # (spec_driven_v1/v2 의 N1/N2 source 불변 — A/B). None 이면 _build_composer 가 계승한
    # base v1 source 로 graceful fallback(tunable composer_prompts_v2 토글).
    composer_answer_spec_source: Any = None
    composer_query_source: Any = None
    composer_slot_v2_source: Any = None
    # spec_driven_v2 — 2-노드(DGX Spark) 분산 변형 전용 프롬프트 source(registry 호스팅,
    # sha 핀). v1 과 격리된 `*_v2` profile_id 를 읽는다(초기엔 v1 fragment 재사용 → 동일
    # sha). None 이면 v2 변형이 run() 에서 부트 배선 오류로 처리(프롬프트 인라인 금지).
    spec_driven_v2_answer_spec_source: Any = None
    spec_driven_v2_query_source: Any = None
    spec_driven_v2_generation_source: Any = None
    spec_driven_v2_triage_source: Any = None
    spec_driven_v2_general_source: Any = None
    spec_driven_v2_verify_source: Any = None
    # spec_driven_v2 Node2 — 슬롯 검증 보조 LLM(SECONDARY_LLM resolve 결과). 변형은
    # 검증을 retrieval.verify_slot 도구로 호출하므로 러너가 직접 쓰진 않으나, 재현 핀
    # (어느 모델이 검증했는가)·테스트 가시성을 위해 번들에 싣는다. 미배선 시 None.
    secondary_llm: Any = None
    summarizer: "ConversationSummarizer | None" = None
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
