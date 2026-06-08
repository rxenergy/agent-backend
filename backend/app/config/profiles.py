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
from app.adapters.tools.document_local import (
    LocalDocumentFetchSectionTool,
    LocalDocumentResolverTool,
)
from app.adapters.tools.document_opensearch import (
    OpenSearchDocumentFetchSectionTool,
    OpenSearchDocumentResolverTool,
)
from app.adapters.tools.memory_approved_stub import ApprovedSearchStubTool
from app.adapters.tools.memory_session_local import SessionLoadTool, SessionUpdateTool
from app.adapters.tools.opensearch_preflight import OpenSearchPreflight
from app.adapters.tools.reranker_local import LocalRerankerTool
from app.adapters.tools.reranker_sparse import SparseRerankerTool
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
from app.application.classification.rule import RuleClassifier
from app.application.memory.summarizer import ConversationSummarizer
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.prompting.classification_source import ClassificationPromptSource
from app.application.prompting.information_need_source import InformationNeedPromptSource
from app.application.prompting.answer_spec_source import AnswerSpecPromptSource
from app.application.prompting.query_translate_source import QueryTranslatePromptSource
from app.application.prompting.finder_source import FinderPromptSource
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


# OpenSearch hybrid 가중치는 *operating point*(최종 결과 개수 = retriever_top_k)에
# 종속된 속성이다. 벤치마크가 [bm25, dense, sparse] 가중치를 고정 k 에 맞춰 튜닝한다.
# 어댑터의 per-call top_k 는 fetch 깊이(fetch_k≈20)라 operating point 가 아니므로,
# pipeline 선택은 반드시 여기(config)에서 결정한다. 미벤치마크 k 는 폴백을 쓴다.
_HYBRID_K_PIPELINES: dict[int, str] = {
    # k=5 전용 pipeline(nrc-hybrid-search-k5)은 실행 클러스터에 등록돼 있지 않아
    # (provisioning 미자동화) preflight 404 를 유발했다. operating point 5 는
    # 폴백 base pipeline(opensearch_search_pipeline=nrc-hybrid-search)을 쓰도록
    # 맵에서 제외한다. 재등록 시 다시 추가. (k10 도 등록 전엔 동일 위험)
    10: "nrc-hybrid-search-k10",  # weights=[0.4, 0.2, 0.4]
}


def _resolve_hybrid_pipeline(retriever_top_k: int, fallback: str | None) -> str | None:
    """operating point(`retriever_top_k`) → 벤치마크 hybrid pipeline.

    벤치마크된 k(5/10)면 전용 pipeline, 그 외엔 `fallback`
    (`opensearch_search_pipeline`). 빈 문자열은 None 으로 정규화."""
    return _HYBRID_K_PIPELINES.get(retriever_top_k, fallback) or None


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
    classification_prompt_source: Any = None
    query_translate_prompt_source: Any = None
    answer_spec_prompt_source: Any = None
    finder_prompt_source: Any = None
    summarizer: ConversationSummarizer | None = None
    retrieval_planner: Any = None
    retrieval_evaluator: Any = None
    retrieval_recoverer: Any = None
    corpus_map: Any = None

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

        # v3.1 Layer 1 범위 한정 — `corpus_map.yaml` (tools/ sibling). 없으면
        # 빈 맵(scope off, noise floor 0) 폴백.
        from app.application.retrieval.corpus_map import CorpusMap

        _corpus_path = Path(settings.tool_registry_path).parent / "corpus_map.yaml"
        corpus_map = (
            CorpusMap.from_yaml(_corpus_path)
            if _corpus_path.is_file()
            else CorpusMap.default()
        )

        # agentic_finder 용어집 — `terminology/vocab.yaml` (tools/ sibling, ISO 25964).
        # 없으면 빈 어휘(canonicalize=passthrough) 폴백. N1.5 terminology.canonicalize
        # (conductor-invoked)가 소비한다. 설계: terminology_normalization_strategy.v1.md.
        from app.application.terminology.vocab import TerminologyVocab

        _vocab_path = (
            Path(settings.tool_registry_path).parent / "terminology" / "vocab.yaml"
        )
        terminology_vocab = (
            TerminologyVocab.from_yaml(_vocab_path)
            if _vocab_path.is_file()
            else TerminologyVocab.default()
        )

        if settings.retriever_backend == "opensearch":
            preflight_severity = _resolve_preflight_severity(settings)
            # Hybrid 가중치 pipeline 은 operating point(retriever_top_k)에 연동.
            # 자세한 근거는 _resolve_hybrid_pipeline / _HYBRID_K_PIPELINES 참조.
            active_search_pipeline = _resolve_hybrid_pipeline(
                settings.retriever_top_k, settings.opensearch_search_pipeline or None
            )
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
                    search_pipeline=active_search_pipeline,
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
                fetch_section_tool = LocalDocumentFetchSectionTool()
                reranker_tool = LocalRerankerTool()  # 임베딩 미가용 폴백 → fake rerank.
                dense_encoder = None  # type: ignore[assignment]

            if dense_encoder is not None:
                # Node 5 reranker — 이미 로드된 sparse encoder(Fermi/SPLADE)를 재사용해
                # query×doc 희소 벡터 내적으로 재정렬한다(모델 기반). 별도 모델 로드 없음.
                reranker_tool = SparseRerankerTool(sparse_encoder)
                retriever_tool = OpenSearchRetrieverTool(
                    endpoint=settings.opensearch_endpoint,
                    index=settings.opensearch_index,
                    dense_encoder=dense_encoder,
                    sparse_encoder=sparse_encoder,
                    search_pipeline=active_search_pipeline,
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
                fetch_section_tool = OpenSearchDocumentFetchSectionTool(
                    endpoint=settings.opensearch_endpoint,
                    index=settings.opensearch_index,
                    username=settings.opensearch_username or None,
                    password=settings.opensearch_password or None,
                    verify_certs=settings.opensearch_verify_certs,
                )
        else:
            retriever_tool = LocalRetrieverTool()
            document_tool = LocalDocumentResolverTool()
            fetch_section_tool = LocalDocumentFetchSectionTool()
            reranker_tool = LocalRerankerTool()  # local 프로필 → 결정론 lexical fake.

        # agentic_finder Finder 도구(설계 finder §3). retrieval.search = 내부 retriever
        # 재사용 + Reranker 정렬(실 cross-encoder 는 배포 시 주입, dev/test 는 identity
        # 폴백 — seam 보존). scope=CorpusMap 결정론, submit_verdict=no-op.
        # 용어 정규화는 N1.5 terminology.canonicalize(conductor-invoked, 용어집 lookup)로
        # 상향(retrieval.normalize 대체). 검색범위 확장(terminology.expand)은 P3.
        from app.adapters.reranker.identity import IdentityReranker
        from app.adapters.tools.retrieval_search import RetrievalSearchTool
        from app.adapters.tools.retrieval_scope import RetrievalScopeTool
        from app.adapters.tools.terminology_canonicalize import TerminologyCanonicalizeTool
        from app.adapters.tools.submit_verdict import SubmitVerdictTool

        tools = {
            "retriever.search": retriever_tool,
            "retrieval.search": RetrievalSearchTool(
                retriever=retriever_tool, reranker=IdentityReranker()
            ),
            "retrieval.scope": RetrievalScopeTool(
                corpus_map=corpus_map,
                tau_high=settings.retrieval_scope_tau_high,
                tau_low=settings.retrieval_scope_tau_low,
                min_token_count=settings.retriever_min_token_count,
            ),
            "terminology.canonicalize": TerminologyCanonicalizeTool(vocab=terminology_vocab),
            "submit_verdict": SubmitVerdictTool(),
            # v3.1 Node 5 reranker — RRF 대체. opensearch 경로는 SPLADE sparse 모델 기반
            # (query×doc 희소 벡터 내적), local 경로는 결정론 lexical fake. 둘 다 동일
            # retriever.rerank 도구 계약이라 dispatcher 무변경.
            "retriever.rerank": reranker_tool,
            "document.resolve_citation": document_tool,
            "document.fetch_section": fetch_section_tool,
            "memory.session_load": SessionLoadTool(session_store),
            "memory.session_update": SessionUpdateTool(
                session_store, ttl_days=settings.memory_session_ttl_days
            ),
            "memory.approved_search": ApprovedSearchStubTool(),
            "verification.citation_check": LocalCitationCheckTool(),
            "verification.faithfulness_check": LocalFaithfulnessCheckTool(),
        }
        tool_executor = ToolExecutor(registry=registry, tools=tools, event_sink=event_sink)

        # 분류 프롬프트 source(registry 호스팅) — boot 시 fragment sha 검증(fail-fast).
        # llm/hybrid backend 와 v3.1 전용 바인딩이 공유한다(인라인 _PROMPT 대체).
        classification_prompt_source = ClassificationPromptSource(
            Path(settings.prompt_local_dir)
        )
        # Node 3 정보 요구 프롬프트 source(registry 호스팅) — 분류와 동일 fail-fast
        # sha 검증. 프롬프트는 코드 인라인이 아니라 registry 에서 관리된다.
        information_need_prompt_source = InformationNeedPromptSource(
            Path(settings.prompt_local_dir)
        )
        # agentic_finder N0 질의 번역 프롬프트 source(registry 호스팅) — 동일 fail-fast
        # sha 검증. 워크플로우 내부는 영어(query_en), 최종 출력만 사용자 언어.
        query_translate_prompt_source = QueryTranslatePromptSource(
            Path(settings.prompt_local_dir)
        )
        # agentic_finder N2 답변 사양 프롬프트 source(registry 호스팅) — 분류/정보요구와
        # 동일 fail-fast sha 검증. 프롬프트는 코드 인라인이 아니라 registry 에서 관리.
        answer_spec_prompt_source = AnswerSpecPromptSource(
            Path(settings.prompt_local_dir)
        )
        # agentic_finder N3 Finder 시스템 프롬프트 source(registry 호스팅) — 동일
        # fail-fast sha 검증. finder_policy_hash 핀의 출처.
        finder_prompt_source = FinderPromptSource(Path(settings.prompt_local_dir))
        if settings.classifier_backend == "rule":
            classifier = RuleClassifier()
        elif settings.classifier_backend == "llm":
            classifier = classification_prompt_source.build_classifier(utility_llm)
        else:
            classifier = HybridClassifier(
                RuleClassifier(),
                classification_prompt_source.build_classifier(utility_llm),
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
        classification_prompt_source=classification_prompt_source,
        information_need_prompt_source=information_need_prompt_source,
        query_translate_prompt_source=query_translate_prompt_source,
        answer_spec_prompt_source=answer_spec_prompt_source,
        finder_prompt_source=finder_prompt_source,
        summarizer=summarizer,
        retrieval_planner=retrieval_planner,
        retrieval_evaluator=retrieval_evaluator,
        retrieval_recoverer=retrieval_recoverer,
        corpus_map=corpus_map,
        tunables={
            "classification_threshold": settings.classification_threshold,
            "verification_citation_threshold": settings.verification_citation_threshold,
            "verification_faithfulness_threshold": settings.verification_faithfulness_threshold,
            "verification_retry_on_fail": settings.verification_retry_on_fail,
            "claim_verification_enabled": settings.claim_verification_enabled,
            "retriever_top_k": settings.retriever_top_k,
            "retriever_min_score": settings.retriever_min_score,
            "retrieval_fetch_k": settings.retrieval_fetch_k,
            # 규제 hard gate(authority_tier) 강제는 v2 스키마 선언 시에만 — v1 은
            # collection-유도 tier 라 vendor(tertiary) 를 차단하면 안 됨.
            "regulatory_hard_gates_enforced": settings.opensearch_schema_version == "v2",
            # v3.1 Layer 1/2 — 범위 confidence 게이트 + 노이즈 floor 기본값.
            "retrieval_scope_tau_high": settings.retrieval_scope_tau_high,
            "retrieval_scope_tau_low": settings.retrieval_scope_tau_low,
            "retriever_min_token_count": settings.retriever_min_token_count,
            # v3.1 P1 Section auto-merge + 예산 거버너.
            "section_merge_max_chunks": settings.section_merge_max_chunks,
            "context_token_budget": settings.context_token_budget,
            "active_cells_mode": settings.active_cells_mode,
            # v3.1 (hierarchical_corrective). Ignored by other variants.
            "llm_call_budget": getattr(settings, "llm_call_budget", 8),
            "citation_contract_path": str(
                Path(settings.prompt_local_dir) / "system" / "citation_contract_v1.md"
            ),
            # agentic_finder N0/N7 — 워크플로우 내부는 영어, 최종 답변만 사용자 언어.
            # 출력-언어 지시문({language} 치환)을 citation contract 와 동일 seam 으로
            # 생성 프롬프트에 prepend 한다(파일 호스팅 → rendered_prompt_hash 에 반영).
            "output_language_contract_path": str(
                Path(settings.prompt_local_dir) / "system" / "output_language_v1.md"
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
