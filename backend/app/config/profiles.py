from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from app.adapters.event_sink_filesystem import FilesystemEventSink
from app.adapters.event_sink_minio import MinioEventSink
from app.adapters.in_memory_session_store import InMemorySessionMemoryStore
from app.adapters.llm_fake import FakeEchoLLM
from app.adapters.llm_http import HttpLLM
from app.adapters.postgres.client import create_pool
from app.adapters.postgres.session_memory_store import PostgresSessionMemoryStore
from app.adapters.tools.artifact_event import WriteEventTool
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
from app.application.agents.sequential_tool_routed_v2 import SequentialToolRoutedRunner
from app.application.classification.hybrid import HybridClassifier
from app.application.classification.llm import LLMClassifier
from app.application.classification.rule import RuleClassifier
from app.application.memory.summarizer import ConversationSummarizer
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import PromptResolver
from app.application.tools.executor import ToolExecutor
from app.application.tools.registry import ToolRegistry
from app.config.settings import Settings
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort
from app.ports.memory_store import SessionMemoryStore


@dataclass
class AppContainer:
    settings: Settings
    runner: Any  # AgentRunner-like
    event_sink: EventSinkPort
    pg_pool: asyncpg.Pool | None = None


def _build_llm(settings: Settings) -> LLMPort:
    provider = settings.llm_provider
    if provider == "fake":
        return FakeEchoLLM()
    if not settings.llm_endpoint or not settings.llm_model:
        raise ValueError(
            f"LLM_PROVIDER={provider} requires LLM_ENDPOINT and LLM_MODEL"
        )
    return HttpLLM(
        provider=provider,
        endpoint=settings.llm_endpoint,
        model=settings.llm_model,
        api_key=settings.llm_api_key or None,
        timeout_s=settings.llm_timeout_s,
        max_attempts=settings.llm_max_attempts,
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


async def build_container(settings: Settings) -> AppContainer:
    event_sink = _build_event_sink(settings)
    recorder = EventRecorder(event_sink, app_profile=settings.app_profile)

    if settings.agent_variant == "fake_echo_v0":
        return AppContainer(
            settings=settings,
            runner=FakeEchoAgentRunner(recorder=recorder),
            event_sink=event_sink,
        )

    if settings.agent_variant != "sequential_tool_routed_v2":
        raise ValueError(f"Unknown AGENT_VARIANT: {settings.agent_variant}")

    # Postgres pool for session memory
    pool: asyncpg.Pool | None = None
    session_store: SessionMemoryStore
    if settings.memory_store == "postgres":
        pool = await create_pool(settings.state_db_url)
        session_store = PostgresSessionMemoryStore(pool)
    else:
        session_store = InMemorySessionMemoryStore()

    # Tool registry + adapters
    registry = ToolRegistry.from_yaml(settings.tool_registry_path)

    if settings.retriever_backend == "opensearch":
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
        "artifact.write_event": WriteEventTool(),
    }
    executor = ToolExecutor(registry=registry, tools=tools, event_sink=event_sink)

    prompt_dir = Path(settings.prompt_local_dir)
    llm = _build_llm(settings)

    classifier: Any
    if settings.classifier_backend == "rule":
        classifier = RuleClassifier()
    elif settings.classifier_backend == "llm":
        classifier = LLMClassifier(llm)
    else:
        classifier = HybridClassifier(
            RuleClassifier(),
            LLMClassifier(llm),
            escalate_below=settings.classifier_escalate_below,
        )

    summarizer = ConversationSummarizer(
        llm=llm,
        enabled=settings.multi_turn_summary_enabled,
        keep_turns=settings.multi_turn_keep_turns,
    )

    runner = SequentialToolRoutedRunner(
        llm=llm,
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
    )

    return AppContainer(
        settings=settings,
        runner=runner,
        event_sink=event_sink,
        pg_pool=pool,
    )


async def shutdown_container(container: AppContainer) -> None:
    if container.pg_pool is not None:
        await container.pg_pool.close()
