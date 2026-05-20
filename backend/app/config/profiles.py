from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx
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
from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.adapters.tools.retriever_opensearch import OpenSearchRetrieverTool
from app.adapters.tools.verification_local import (
    LocalCitationCheckTool,
    LocalFaithfulnessCheckTool,
)
from app.application.agents.fake_echo_v0 import FakeEchoAgentRunner
from app.application.agents.llm_router import LLMRouter
from app.application.agents.sequential_tool_routed_v2 import SequentialToolRoutedRunner
from app.ports.agent_runner import AgentRunner
from app.application.classification.hybrid import HybridClassifier
from app.application.classification.llm import LLMClassifier
from app.application.classification.rule import RuleClassifier
from app.application.memory.summarizer import ConversationSummarizer
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import PromptResolver
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.config.settings import LLMPoolEntry, Settings
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort
from app.ports.memory_store import SessionMemoryStore


@dataclass
class AppContainer:
    settings: Settings
    runners: dict[str, AgentRunner] = field(default_factory=dict)
    llm_pool: dict[str, LLMPort] = field(default_factory=dict)
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


async def _opensearch_preflight(endpoint: str, index: str) -> None:
    """Best-effort reachability + index existence check.

    Logs structured warnings and swallows errors — Agent boot continues regardless
    so local development isn't blocked when OpenSearch or the seed is missing.
    """
    log = structlog.get_logger("opensearch.preflight")
    base = endpoint.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            health = await client.get(f"{base}/_cluster/health")
            if health.status_code >= 400:
                log.warning(
                    "opensearch_health_unreachable",
                    endpoint=base,
                    status=health.status_code,
                )
                return
            head = await client.request("HEAD", f"{base}/{index}")
            if head.status_code == 404:
                log.warning(
                    "opensearch_index_missing",
                    endpoint=base,
                    index=index,
                    hint="run `make seed` to create and populate the index",
                )
                return
            if head.status_code >= 400:
                log.warning(
                    "opensearch_index_check_failed",
                    endpoint=base,
                    index=index,
                    status=head.status_code,
                )
                return
        log.info("opensearch_preflight_ok", endpoint=base, index=index)
    except httpx.RequestError as exc:
        log.warning(
            "opensearch_preflight_unreachable",
            endpoint=base,
            index=index,
            error=str(exc),
        )


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


KNOWN_VARIANTS: frozenset[str] = frozenset(
    {FakeEchoAgentRunner.variant_id, SequentialToolRoutedRunner.variant_id}
)


def _validate_variants(enabled: list[str]) -> None:
    unknown = set(enabled) - KNOWN_VARIANTS
    if unknown:
        raise ValueError(f"Unknown agent variants enabled: {sorted(unknown)}")


async def build_container(settings: Settings) -> AppContainer:
    _validate_variants(settings.agent_variants_enabled)
    if settings.default_variant not in settings.agent_variants_enabled:
        raise ValueError(
            f"default_variant={settings.default_variant!r} not in agent_variants_enabled"
        )

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

    runners: dict[str, AgentRunner] = {}

    if FakeEchoAgentRunner.variant_id in settings.agent_variants_enabled:
        runners[FakeEchoAgentRunner.variant_id] = FakeEchoAgentRunner(recorder=recorder)

    if SequentialToolRoutedRunner.variant_id in settings.agent_variants_enabled:
        # Postgres pool for session memory
        pool: asyncpg.Pool | None = None
        session_store: SessionMemoryStore
        if settings.memory_store == "postgres":
            pool = await create_pool(settings.state_db_url)
            session_store = PostgresSessionMemoryStore(pool)
        else:
            session_store = InMemorySessionMemoryStore()

        registry = ToolRegistry.from_yaml(settings.tool_registry_path)

        if settings.retriever_backend == "opensearch":
            await _opensearch_preflight(
                settings.opensearch_endpoint, settings.opensearch_index
            )
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
        executor = ToolExecutor(registry=registry, tools=tools, event_sink=event_sink)

        prompt_dir = Path(settings.prompt_local_dir)

        classifier: Any
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

        runners[SequentialToolRoutedRunner.variant_id] = SequentialToolRoutedRunner(
            llm_router=llm_router,
            tool_executor=executor,
            prompt_resolver=PromptResolver(str(prompt_dir), label=settings.prompt_label),
            prompt_renderer=PromptRenderer(prompt_dir=prompt_dir),
            context_builder=ContextBuilder(capture_mode=settings.context_capture_mode),
            recorder=recorder,
            event_sink=event_sink,
            app_profile=settings.app_profile,
            classifier=classifier,
            classification_threshold=settings.classification_threshold,
            verification_citation_threshold=settings.verification_citation_threshold,
            verification_faithfulness_threshold=settings.verification_faithfulness_threshold,
            verification_retry_on_fail=settings.verification_retry_on_fail,
            summarizer=summarizer,
            retriever_top_k=settings.retriever_top_k,
            retriever_min_score=settings.retriever_min_score,
        )
    else:
        pool = None  # type: ignore[assignment]

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
        llm_pool=llm_pool,
        event_sink=event_sink,
        pg_pool=pool,
    )


async def shutdown_container(container: AppContainer) -> None:
    if container.pg_pool is not None:
        await container.pg_pool.close()
