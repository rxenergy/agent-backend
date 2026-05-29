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
    retrieval_planner: Any = None
    retrieval_evaluator: Any = None
    retrieval_recoverer: Any = None

    if needs_tool_stack:
        session_store: SessionMemoryStore
        if settings.memory_store == "postgres":
            pool = await create_pool(settings.state_db_url)
            session_store = PostgresSessionMemoryStore(pool)
        else:
            session_store = InMemorySessionMemoryStore()

        registry = ToolRegistry.from_yaml(settings.tool_registry_path)

        # v3.1 Node 4 planner — `retrieval_strategies.yaml` (tools/ sibling of
        # the tool registry). 없으면 단일 hybrid 폴백(변형이 default() 사용).
        from app.application.retrieval.planner import RetrievalPlanner

        _strategies_path = Path(settings.tool_registry_path).parent / "retrieval_strategies.yaml"
        if _strategies_path.is_file():
            retrieval_planner = RetrievalPlanner.from_yaml(_strategies_path)

        # v3.1 Node 6 evaluator — `evaluator_policy.yaml` (tools/ sibling).
        from app.application.retrieval.evaluator import RetrievalEvaluator

        _policy_path = Path(settings.tool_registry_path).parent / "evaluator_policy.yaml"
        if _policy_path.is_file():
            retrieval_evaluator = RetrievalEvaluator.from_yaml(_policy_path)

        # v3.1 Node 7 recover — data/synonyms/ (repo 루트). 없으면 빈 사전 폴백.
        from app.application.retrieval.recovery import RetrievalRecoverer

        _syn_dir = Path(settings.tool_registry_path).parent.parent / "data" / "synonyms"
        retrieval_recoverer = RetrievalRecoverer.from_yaml_dir(
            _syn_dir, max_rounds=settings.retrieval_max_recover_rounds
        )

        if settings.retriever_backend == "opensearch":
            preflight_severity = _resolve_preflight_severity(settings)
            # v3.1: the G3 evaluator reads regulatory-meta fields. These exist
            # only in the v2 schema — the active v1 data has not been
            # re-ingested with them. The judgment of "is v2 usable" is the
            # *declared* `opensearch_schema_version` flag (single source of
            # truth), NOT the index name — the name is arbitrary and tells
            # nothing about populated data. Require the fields only when BOTH
            # the hierarchical_corrective variant is enabled AND the deployment
            # declares schema v2; on v1 we ask for nothing so boot is
            # unaffected. See infra/opensearch/mappings/README.md.
            required_fields: tuple[str, ...] = ()
            if (
                "hierarchical_corrective_v3_1" in settings.agent_variants_enabled
                and settings.opensearch_schema_version == "v2"
            ):
                required_fields = (
                    "clause_id", "authority_tier", "jurisdiction", "effective_on",
                )
            preflight_checks: list[PreflightCheck] = [
                OpenSearchPreflight(
                    endpoint=settings.opensearch_endpoint,
                    index=settings.opensearch_index,
                    severity=preflight_severity,
                    search_pipeline=settings.opensearch_search_pipeline or None,
                    required_fields=required_fields,
                    verify_certs=settings.opensearch_verify_certs,
                )
            ]
            # `strict` raises `PreflightFailedError` and aborts container boot
            # (12-Factor §IV / K8s startup-probe semantics).
            await PreflightRunner(preflight_checks).run_all()

            # Heavy ML deps (torch/sentence-transformers/transformers) load only
            # on this branch. Failure to load is governed by the same severity
            # as the rest of preflight: `warn` keeps boot alive, `strict` aborts.
            from app.adapters.embeddings.e5 import E5Encoder
            from app.adapters.embeddings.fermi import FermiEncoder

            boot_log = structlog.get_logger("retriever.boot")
            try:
                dense_encoder = E5Encoder(
                    model_id=settings.embedding_e5_model,
                    device=settings.embedding_device,
                    max_seq_len=settings.embedding_e5_max_seq_len,
                )
                sparse_encoder = FermiEncoder(
                    model_id=settings.embedding_fermi_model,
                    device=settings.embedding_device,
                    max_seq_len=settings.embedding_fermi_max_seq_len,
                    top_n=settings.embedding_fermi_top_n,
                )
                dense_encoder.warmup()
                sparse_encoder.warmup()
                boot_log.info(
                    "embedding_models_loaded",
                    e5=settings.embedding_e5_model,
                    fermi=settings.embedding_fermi_model,
                    device=settings.embedding_device,
                    dense_dim=dense_encoder.dim,
                )
            except Exception as exc:
                if preflight_severity == "strict":
                    raise
                boot_log.warning(
                    "embedding_models_load_failed",
                    error=str(exc),
                    hint="hybrid retrieval disabled; container boots with local retriever fallback",
                )
                retriever_tool = LocalRetrieverTool()
                document_tool = LocalDocumentResolverTool()
                dense_encoder = None  # type: ignore[assignment]

            if dense_encoder is not None:
                retriever_tool = OpenSearchRetrieverTool(
                    endpoint=settings.opensearch_endpoint,
                    index=settings.opensearch_index,
                    dense_encoder=dense_encoder,
                    sparse_encoder=sparse_encoder,
                    search_pipeline=settings.opensearch_search_pipeline or None,
                    dense_field=settings.opensearch_dense_field,
                    sparse_field=settings.opensearch_sparse_field,
                    text_field=settings.opensearch_text_field,
                    k_dense=settings.retriever_k_dense,
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
        retrieval_planner=retrieval_planner,
        retrieval_evaluator=retrieval_evaluator,
        retrieval_recoverer=retrieval_recoverer,
        tunables={
            "classification_threshold": settings.classification_threshold,
            "verification_citation_threshold": settings.verification_citation_threshold,
            "verification_faithfulness_threshold": settings.verification_faithfulness_threshold,
            "verification_retry_on_fail": settings.verification_retry_on_fail,
            "retriever_top_k": settings.retriever_top_k,
            "retriever_min_score": settings.retriever_min_score,
            "retrieval_fetch_k": settings.retrieval_fetch_k,
            # 규제 hard gate(authority_tier) 강제는 v2 스키마 선언 시에만 — v1 은
            # collection-유도 tier 라 vendor(tertiary) 를 차단하면 안 됨.
            "regulatory_hard_gates_enforced": settings.opensearch_schema_version == "v2",
            "active_cells_mode": settings.active_cells_mode,
            # v3.1 (hierarchical_corrective). Ignored by other variants.
            "llm_call_budget": getattr(settings, "llm_call_budget", 8),
            "citation_contract_path": str(
                Path(settings.prompt_local_dir) / "system" / "citation_contract_v1.md"
            ),
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
