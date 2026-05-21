from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import structlog

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.event_sink.minio import MinioEventSink
from app.adapters.session_store.in_memory import InMemorySessionMemoryStore
from app.adapters.llm.fake import FakeEchoLLM
from app.adapters.llm.http import HttpLLM
from app.adapters.postgres.client import create_pool
from app.adapters.postgres.session_memory_store import PostgresSessionMemoryStore
from app.adapters.tools.document_local import LocalDocumentResolverTool
from app.adapters.tools.document_opensearch import OpenSearchDocumentResolverTool
from app.adapters.tools.memory_approved_stub import ApprovedSearchStubTool
from app.adapters.tools.memory_session_local import SessionLoadTool, SessionUpdateTool
from app.adapters.tools.opensearch_preflight import OpenSearchPreflight
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.retriever_opensearch import OpenSearchRetrieverTool
from app.application.preflight.port import PreflightCheck
from app.application.preflight.runner import PreflightRunner
from app.adapters.tools.verification_local import (
    LocalCitationCheckTool,
    LocalFaithfulnessCheckTool,
)
# Side-effect import: triggers `@register_variant(...)` for every shipped variant.
import app.application.agents  # noqa: F401
from app.application.agents.llm_router import LLMRouter
from app.application.agents.registry import AgentDeps, VariantRegistry
from app.ports.agent_runner import AgentRunner
from app.application.classification.hybrid import HybridClassifier
from app.application.classification.llm import LLMClassifier
from app.application.classification.rule import RuleClassifier
from app.application.memory.summarizer import ConversationSummarizer
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.hybrid_source import HybridPromptSource
from app.application.prompting.local_source import LocalPromptSource
from app.application.prompting.phoenix_source import (
    PhoenixPromptSource,
    build_phoenix_client,
)
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import PromptResolver
from app.ports.prompt_source import PromptSourcePort
from app.application.agents.variant_spec import VariantSpecRegistry
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.config.settings import LLMPoolEntry, Settings
from app.domain.agents import VariantSpec
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort
from app.ports.memory_store import SessionMemoryStore


@dataclass
class AppContainer:
    settings: Settings
    runners: dict[str, AgentRunner] = field(default_factory=dict)
    llm_pool: dict[str, LLMPort] = field(default_factory=dict)
    variant_specs: dict[str, VariantSpec] = field(default_factory=dict)
    event_sink: EventSinkPort | None = None
    pg_pool: asyncpg.Pool | None = None


def _build_http_llm(entry: LLMPoolEntry) -> HttpLLM:
    api_key = os.getenv(entry.api_key_env) if entry.api_key_env else None
    return HttpLLM(
        provider=entry.provider,
        endpoint=entry.endpoint,
        model=entry.model,
        api_key=api_key,
        timeout_s=entry.timeout_s,
        max_attempts=entry.max_attempts,
    )


def _build_llm_pool(settings: Settings) -> dict[str, LLMPort]:
    """`fake-echo` is always present. Additional entries come from `LLM_POOL` env."""
    pool: dict[str, LLMPort] = {"fake-echo": FakeEchoLLM(model_id="fake-echo")}
    for entry in settings.llm_pool:
        if entry.id in pool:
            raise ValueError(f"duplicate llm_pool id: {entry.id!r}")
        pool[entry.id] = _build_http_llm(entry)
    return pool


def _resolve_preflight_severity(settings: Settings) -> "str":
    """ADR-0007: derive severity from profile when `preflight_mode=auto`.

    - `local` → `warn` (dev boot must survive missing seed / cluster).
    - `aws-mvp`, `onprem` → `strict` (operational boot must fail-fast).
    """
    if settings.preflight_mode != "auto":
        return settings.preflight_mode
    return "warn" if settings.app_profile == "local" else "strict"


def _build_event_sink(settings: Settings) -> EventSinkPort:
    if settings.event_sink == "filesystem":
        return FilesystemEventSink(
            root=settings.event_filesystem_root,
            prefix=settings.event_prefix,
        )
    endpoint_url = settings.minio_endpoint if settings.event_sink == "minio" else None
    access_key = settings.minio_access_key if settings.event_sink == "minio" else None
    secret_key = settings.minio_secret_key if settings.event_sink == "minio" else None
    return MinioEventSink(
        bucket=settings.event_bucket,
        prefix=settings.event_prefix,
        endpoint_url=endpoint_url,
        access_key=access_key,
        secret_key=secret_key,
        region=settings.aws_region,
    )


def _build_prompt_resolver(settings: Settings, prompt_dir: Path) -> PromptResolver:
    """Wire the configured prompt source (local | phoenix | hybrid) into a resolver.

    Mirrors the `retriever_backend` factory pattern: env-driven dispatch, no
    branching inside the agent runner. Phoenix client import is lazy so the
    `arize-phoenix-client` extra stays optional for local development.
    """
    log = structlog.get_logger("prompting.boot")
    label = settings.prompt_label
    local = LocalPromptSource(prompt_dir, label=label)

    if settings.prompt_source == "local":
        log.info("prompt_source_selected", source="local", dir=str(prompt_dir))
        return PromptResolver(local)

    if settings.prompt_source == "phoenix":
        client = build_phoenix_client(settings.phoenix_endpoint)
        log.info("prompt_source_selected", source="phoenix", endpoint=settings.phoenix_endpoint)
        return PromptResolver(PhoenixPromptSource(client, label=label))

    # hybrid: Phoenix primary, Local fallback.
    primary: PromptSourcePort
    try:
        primary = PhoenixPromptSource(
            build_phoenix_client(settings.phoenix_endpoint), label=label
        )
    except ImportError as exc:
        log.warning(
            "prompt_source_phoenix_unavailable",
            error=str(exc),
            fallback="local",
        )
        return PromptResolver(local)
    log.info(
        "prompt_source_selected",
        source="hybrid",
        primary="phoenix",
        fallback="local",
    )
    return PromptResolver(HybridPromptSource(primary=primary, fallback=local))


def _validate_variants(enabled: list[str]) -> None:
    """Validate against `VariantRegistry.known()` (ADR-0004).

    Membership is derived from `@register_variant(...)` decorators, populated
    when `app.application.agents` is imported at module top.
    """
    known = VariantRegistry.known()
    unknown = set(enabled) - known
    if unknown:
        raise ValueError(
            f"Unknown agent variants enabled: {sorted(unknown)}; "
            f"registered={sorted(known)}"
        )


async def build_container(settings: Settings) -> AppContainer:
    _validate_variants(settings.agent_variants_enabled)
    if settings.default_variant not in settings.agent_variants_enabled:
        raise ValueError(
            f"default_variant={settings.default_variant!r} not in agent_variants_enabled"
        )

    # ADR-0006: variant capability metadata is loaded from YAML before any
    # runner is constructed so `runner.spec` is the single typed source for
    # variant_id / compatible_llms / required_tools / capability_tags.
    spec_registry = VariantSpecRegistry.from_yaml(settings.variant_registry_path)
    variant_specs: dict[str, VariantSpec] = {
        vid: spec_registry.get(vid) for vid in settings.agent_variants_enabled
    }

    event_sink = _build_event_sink(settings)
    recorder = EventRecorder(event_sink, app_profile=settings.app_profile)

    # LLM pool + routers (default for Node 8, utility for classifier/summarizer)
    llm_pool = _build_llm_pool(settings)
    if settings.default_llm not in llm_pool:
        raise ValueError(
            f"default_llm={settings.default_llm!r} not in pool ids={sorted(llm_pool)}"
        )
    if settings.utility_llm not in llm_pool:
        raise ValueError(
            f"utility_llm={settings.utility_llm!r} not in pool ids={sorted(llm_pool)}"
        )
    llm_router = LLMRouter(pool=llm_pool, default_id=settings.default_llm)
    utility_llm = llm_pool[settings.utility_llm]

    # Heavy deps (postgres pool / tool executor / prompt resolver / classifier /
    # summarizer) are constructed only when at least one enabled variant
    # declares non-empty `required_tools` (YAML). This keeps `fake_echo_v0`-only
    # boots free of postgres / opensearch dependencies.
    needs_tool_stack = any(
        spec.required_tools for spec in variant_specs.values()
    )

    pool: asyncpg.Pool | None = None
    tool_executor: ToolExecutor | None = None
    prompt_resolver: PromptResolver | None = None
    prompt_renderer: PromptRenderer | None = None
    context_builder: ContextBuilder | None = None
    classifier: Any = None
    summarizer: ConversationSummarizer | None = None

    if needs_tool_stack:
        session_store: SessionMemoryStore
        if settings.memory_store == "postgres":
            pool = await create_pool(settings.state_db_url)
            session_store = PostgresSessionMemoryStore(pool)
        else:
            session_store = InMemorySessionMemoryStore()

        registry = ToolRegistry.from_yaml(settings.tool_registry_path)

        if settings.retriever_backend == "opensearch":
            preflight_severity = _resolve_preflight_severity(settings)
            preflight_checks: list[PreflightCheck] = [
                OpenSearchPreflight(
                    endpoint=settings.opensearch_endpoint,
                    index=settings.opensearch_index,
                    severity=preflight_severity,
                    verify_certs=settings.opensearch_verify_certs,
                )
            ]
            # `strict` raises `PreflightFailedError` and aborts container boot
            # (12-Factor §IV / K8s startup-probe semantics).
            await PreflightRunner(preflight_checks).run_all()
            retriever_tool = OpenSearchRetrieverTool(
                endpoint=settings.opensearch_endpoint,
                index=settings.opensearch_index,
                username=settings.opensearch_username or None,
                password=settings.opensearch_password or None,
                verify_certs=settings.opensearch_verify_certs,
            )
            document_tool = OpenSearchDocumentResolverTool(
                endpoint=settings.opensearch_endpoint,
                index=settings.opensearch_index,
                username=settings.opensearch_username or None,
                password=settings.opensearch_password or None,
                verify_certs=settings.opensearch_verify_certs,
            )
        else:
            retriever_tool = LocalRetrieverTool()
            document_tool = LocalDocumentResolverTool()

        tools = {
            "retriever.search": retriever_tool,
            "document.resolve_citation": document_tool,
            "memory.session_load": SessionLoadTool(session_store),
            "memory.session_update": SessionUpdateTool(
                session_store, ttl_days=settings.memory_session_ttl_days
            ),
            "memory.approved_search": ApprovedSearchStubTool(),
            "verification.citation_check": LocalCitationCheckTool(),
            "verification.faithfulness_check": LocalFaithfulnessCheckTool(),
        }
        tool_executor = ToolExecutor(registry=registry, tools=tools, event_sink=event_sink)

        if settings.classifier_backend == "rule":
            classifier = RuleClassifier()
        elif settings.classifier_backend == "llm":
            classifier = LLMClassifier(utility_llm)
        else:
            classifier = HybridClassifier(
                RuleClassifier(),
                LLMClassifier(utility_llm),
                escalate_below=settings.classifier_escalate_below,
            )

        summarizer = ConversationSummarizer(
            llm=utility_llm,
            enabled=settings.multi_turn_summary_enabled,
            keep_turns=settings.multi_turn_keep_turns,
        )

        prompt_resolver = _build_prompt_resolver(settings, Path(settings.prompt_local_dir))
        prompt_renderer = PromptRenderer()
        context_builder = ContextBuilder(capture_mode=settings.context_capture_mode)

    # ADR-0004: dispatch to each enabled variant's registered factory.
    deps = AgentDeps(
        recorder=recorder,
        event_sink=event_sink,
        app_profile=settings.app_profile,
        llm_router=llm_router,
        utility_llm=utility_llm,
        tool_executor=tool_executor,
        prompt_resolver=prompt_resolver,
        prompt_renderer=prompt_renderer,
        context_builder=context_builder,
        classifier=classifier,
        summarizer=summarizer,
        tunables={
            "classification_threshold": settings.classification_threshold,
            "verification_citation_threshold": settings.verification_citation_threshold,
            "verification_faithfulness_threshold": settings.verification_faithfulness_threshold,
            "verification_retry_on_fail": settings.verification_retry_on_fail,
            "retriever_top_k": settings.retriever_top_k,
            "retriever_min_score": settings.retriever_min_score,
            "active_cells_mode": settings.active_cells_mode,
        },
    )
    runners: dict[str, AgentRunner] = {
        vid: VariantRegistry.build(vid, variant_specs[vid], deps)
        for vid in settings.agent_variants_enabled
    }

    log = structlog.get_logger("agent.boot")
    log.info(
        "container_built",
        runners=sorted(runners.keys()),
        llm_pool=sorted(llm_pool.keys()),
        default_variant=settings.default_variant,
        default_llm=settings.default_llm,
        utility_llm=settings.utility_llm,
    )

    return AppContainer(
        settings=settings,
        runners=runners,
        variant_specs=variant_specs,
        llm_pool=llm_pool,
        event_sink=event_sink,
        pg_pool=pool,
    )


async def shutdown_container(container: AppContainer) -> None:
    if container.pg_pool is not None:
        await container.pg_pool.close()
