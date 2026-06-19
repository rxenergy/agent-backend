from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import structlog

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.event_sink.minio import MinioEventSink
from app.adapters.session_store.in_memory import InMemorySessionStateStore
from app.adapters.llm.fake import FakeEchoLLM
from app.adapters.llm.http import HttpLLM
from app.adapters.postgres.client import create_pool
from app.adapters.postgres.session_state_store import PostgresSessionStateStore
from app.adapters.tools.document_local import (
    LocalDocumentFetchChunksTool,
    LocalDocumentFetchSectionTool,
    LocalDocumentResolverTool,
)
from app.adapters.tools.document_opensearch import (
    OpenSearchDocumentFetchChunksTool,
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
from app.application.prompting.spec_driven_source import (
    ComposerAnswerSpecSource,
    ComposerPersonaSource,
    ComposerQuerySource,
    ComposerSlotSource,
    ComposerSlotV2Source,
    ComposerSlotVerifySource,
    ComposerSynthesizeSource,
    SpecDrivenAnswerSpecSource,
    SpecDrivenGeneralSource,
    SpecDrivenGenerationSource,
    SpecDrivenQuerySource,
    SpecDrivenRescopeSource,
    SpecDrivenTriageSource,
    SpecDrivenVerifySource,
)
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
from app.ports.session_state_store import SessionStateStore


@dataclass
class AppContainer:
    settings: Settings
    runners: dict[str, AgentRunner] = field(default_factory=dict)
    llm_pool: dict[str, LLMPort] = field(default_factory=dict)
    variant_specs: dict[str, VariantSpec] = field(default_factory=dict)
    event_sink: EventSinkPort | None = None
    pg_pool: asyncpg.Pool | None = None


def _build_http_llm(entry: LLMPoolEntry, *, default_region: str) -> HttpLLM:
    api_key = os.getenv(entry.api_key_env) if entry.api_key_env else None
    return HttpLLM(
        provider=entry.provider,
        endpoint=entry.endpoint,
        model=entry.model,
        api_key=api_key,
        timeout_s=entry.timeout_s,
        max_attempts=entry.max_attempts,
        # bedrock 은 region 에서 endpoint 를 유도한다. entry.region 우선, 미설정 시
        # 프로파일의 aws_region 폴백(aws-mvp EC2 IAM role 시나리오와 일치).
        region=entry.region or default_region,
    )


def _build_llm_pool(settings: Settings) -> dict[str, LLMPort]:
    """`fake-echo` is always present. Additional entries come from `LLM_POOL` env."""
    pool: dict[str, LLMPort] = {"fake-echo": FakeEchoLLM(model_id="fake-echo")}
    for entry in settings.llm_pool:
        if entry.id in pool:
            raise ValueError(f"duplicate llm_pool id: {entry.id!r}")
        pool[entry.id] = _build_http_llm(entry, default_region=settings.aws_region)
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
    # utility_llm 미지정(빈 값)이면 default_llm 로 폴백 — UTILITY_LLM 을 안 잡으면
    # 분류기/요약/translate/answer_spec 이 fake-echo 에 묶여 confidence 0 으로
    # 무너지던 함정을 제거한다. 부트 시 단일 모델로 핀되므로(요청별 model 추종이
    # 아님) 재현성(원칙 5)·결정성은 보존된다. default_llm 은 233 행에서 이미 풀 검증됨.
    utility_llm_id = settings.utility_llm or settings.default_llm
    if utility_llm_id not in llm_pool:
        raise ValueError(
            f"utility_llm={utility_llm_id!r} not in pool ids={sorted(llm_pool)}"
        )
    # spec_driven_v2 Node2(sub) — 외부참조 선별(retrieval.follow_up)을 도는 보조 LLM
    # (SECONDARY_LLM). 빈 값이면 default_llm 폴백(단일노드 graceful — Node2 가 없으면
    # follow_up 을 Node1 에서 도는 셈이나 도구 배선은 openai_compat 게이트가 따로 막는다).
    # 설정됐으나 pool 에 없으면 fail-fast(오타 방지 — utility_llm 검증과 동형, 원칙 5 부트 결정성).
    secondary_llm_id = settings.secondary_llm or settings.default_llm
    if secondary_llm_id not in llm_pool:
        raise ValueError(
            f"secondary_llm={secondary_llm_id!r} not in pool ids={sorted(llm_pool)}"
        )
    llm_router = LLMRouter(pool=llm_pool, default_id=settings.default_llm)
    utility_llm = llm_pool[utility_llm_id]
    secondary_llm = llm_pool[secondary_llm_id]

    # Heavy deps (postgres pool / tool executor / prompt resolver / classifier /
    # summarizer) are constructed only when at least one enabled variant
    # declares non-empty `required_tools` (YAML). This keeps tool-less boots
    # free of postgres / opensearch dependencies.
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
    spec_driven_answer_spec_source: Any = None
    spec_driven_query_source: Any = None
    spec_driven_generation_source: Any = None
    spec_driven_triage_source: Any = None
    spec_driven_general_source: Any = None
    composer_slot_source: Any = None
    composer_synthesize_source: Any = None
    composer_slot_verify_source: Any = None
    # composer v2 — 책임 재분배(split.design.v1): N1 답변설계 / N2 검색설계 / 슬롯 role 소비.
    composer_answer_spec_source: Any = None
    composer_query_source: Any = None
    composer_slot_v2_source: Any = None
    # composer 다중 페르소나(composer_persona_framework.design.v1 §10) — persona_id → profile
    # fragment source. 미배선(빈 dict)이면 페르소나 variant 가 profile 없이 graceful.
    composer_persona_sources: dict[str, Any] = {}
    composer_rescope_source: Any = None
    summarizer: ConversationSummarizer | None = None
    corpus_map: Any = None

    if needs_tool_stack:
        session_store: SessionStateStore
        if settings.memory_store == "postgres":
            pool = await create_pool(settings.state_db_url)
            session_store = PostgresSessionStateStore(pool)
        else:
            session_store = InMemorySessionStateStore()

        registry = ToolRegistry.from_yaml(settings.tool_registry_path)

        # 검색 범위 한정(corpus_map) — `corpus_map.yaml` (tools/ sibling). retrieval.scope
        # 도구(결정론 scope 보강)가 소비한다. 없으면 빈 맵(scope off, noise floor 0) 폴백.
        from app.application.retrieval.corpus_map import CorpusMap

        _corpus_path = Path(settings.tool_registry_path).parent / "corpus_map.yaml"
        corpus_map = (
            CorpusMap.from_yaml(_corpus_path)
            if _corpus_path.is_file()
            else CorpusMap.default()
        )

        # 용어집 — `terminology/vocab.yaml` (tools/ sibling, ISO 25964). 없으면 빈 어휘
        # (canonicalize=passthrough) 폴백. terminology.canonicalize/expand 도구가 소비한다.
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
            # spec_driven_v1 reads no regulatory-meta preflight fields — the
            # active variant works off authority_tier(collection)/clause_id
            # exact-match at query time, not a boot-time schema gate. Require no
            # fields so boot is schema-version agnostic.
            required_fields: tuple[str, ...] = ()
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
                fetch_chunks_tool = LocalDocumentFetchChunksTool()
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
                    snippet_chars=settings.opensearch_snippet_chars,
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
                fetch_chunks_tool = OpenSearchDocumentFetchChunksTool(
                    endpoint=settings.opensearch_endpoint,
                    index=settings.opensearch_index,
                    username=settings.opensearch_username or None,
                    password=settings.opensearch_password or None,
                    verify_certs=settings.opensearch_verify_certs,
                    snippet_chars=settings.opensearch_snippet_chars,
                )
        else:
            retriever_tool = LocalRetrieverTool()
            document_tool = LocalDocumentResolverTool()
            fetch_section_tool = LocalDocumentFetchSectionTool()
            fetch_chunks_tool = LocalDocumentFetchChunksTool()
            reranker_tool = LocalRerankerTool()  # local 프로필 → 결정론 lexical fake.

        # 검색-검증 도구(verify_slot/follow_up)가 구조화 출력(JSON)을 낼 수 있는 provider.
        # - openai_compat(내부망 vLLM): guided_json(JSON-schema guided decoding)으로 강제.
        # - bedrock/anthropic(Claude Haiku 등): guided decoding 은 없으나 프롬프트가 JSON 출력을
        #   지시하고 어댑터가 json.loads 로 파싱한다(Haiku 가 JSON 지시를 잘 따름). second 노드를
        #   사내망 vLLM 대신 AWS Bedrock Haiku 로 둘 때(vLLM 서브 미연결) 검증/외부참조가 실제로
        #   돌게 한다. fake 엔트리만 비대상(구조화 출력 불가 → graceful skip → 단일노드 degrade).
        _STRUCTURED_PROVIDERS = ("openai_compat", "bedrock", "anthropic")

        # retrieval.follow_up — 1차 검색 청크에서 외부 참조 추출 + 재검색 쿼리 생성.
        # spec_driven_v2/composer_pipelined 의 **sub 노드**(Node2 = SECONDARY_LLM =
        # secondary_llm, 없으면 default_llm 폴백) HttpLLM 을 그대로 주입해 재사용한다 — 구조화
        # 출력 가능 provider(_STRUCTURED_PROVIDERS) 엔트리일 때만. 연결 정보(endpoint/model/
        # api_key/timeout·재시도·에러매핑)는 HttpLLM 이 단독 소유하고, RefSettings 는 추출 knob
        # (max_output_tokens·schema 경로)만 from_env 기본값으로 쓴다 — 더 이상 DOCUMENTS_REF_VLLM_*
        # 연결 env 를 이중 구성하지 않는다.
        follow_up_tool = None
        _follow_up_entry = next(
            (e for e in settings.llm_pool if e.id == secondary_llm_id), None
        )
        if _follow_up_entry is not None and _follow_up_entry.provider in _STRUCTURED_PROVIDERS:
            try:
                from app.adapters.ref_extractor_llm import LlmRefExtractor
                from app.adapters.tools.retrieval_follow_up import RetrievalFollowUpTool
                from app.adapters.ref_postprocess.settings import RefSettings as _RefSettings

                # 동시성 캡(_MAX_CONCURRENCY, 기본 8)은 슬롯 fan-out 전체에 걸친 전역값 —
                # 청크별 독립 HTTP 요청을 동시에 쏴 vLLM continuous batching 으로 묶어 처리한다.
                # ceil(N/conc) 라운드 × per-call timeout 이 registry 예산(100s) 안에 들도록 두되,
                # KV cache 경쟁이 보이면 DOCUMENTS_REF_MAX_CONCURRENCY 로 낮춘다. per-call
                # timeout·재시도는 HttpLLM(pool 엔트리 timeout_s·max_attempts)이 소유한다.
                _ref_extractor = LlmRefExtractor(
                    llm=secondary_llm,
                    settings=_RefSettings.from_env(),
                    catalog_csv_path=Path(os.environ.get(
                        "DOCUMENTS_METADATA_CSV", "/app/data/ref/metadata_unified.csv"
                    )),
                    cache_path=Path(os.environ.get(
                        "DOCUMENTS_METADATA_CACHE", "/app/data/ref/_ref_catalog.json"
                    )),
                )
                _fu_conc = os.environ.get("DOCUMENTS_REF_MAX_CONCURRENCY")
                follow_up_tool = RetrievalFollowUpTool(
                    ref_extractor=_ref_extractor,
                    max_concurrency=int(_fu_conc) if _fu_conc else None,
                )
            except Exception as _exc:
                structlog.get_logger("retrieval.follow_up.boot").warning(
                    "follow_up_tool_disabled",
                    error=str(_exc),
                    llm_id=secondary_llm_id,
                    hint="secondary_llm provider supports structured output but tool init "
                         "failed; graceful degrade",
                )

        # retrieval.verify_slot — spec_driven_v2 슬롯 검증을 follow_up 과 같은 **sub 노드**
        # (Node2 = secondary_llm, 없으면 default_llm 폴백)에서 돈다. follow_up 과 동일 가드:
        # secondary_llm pool 엔트리가 openai_compat(내부망 vLLM)일 때만 배선한다(verify 는
        # vLLM guided_json 에 의존 — anthropic/fake 비호환 → graceful skip). 토글
        # (spec_driven_v2_verify_enabled)이 꺼져 있어도 미배선(단일노드 degrade). 연결 정보
        # (endpoint/model/timeout·재시도)는 secondary_llm(HttpLLM)이 단독 소유한다. verify 와
        # follow_up 이 같은 Node2 에 몰리므로 두 도구 fan-out 은 한 vLLM 을 공유한다(아래 캡 주석).
        verify_slot_tool = None
        _verify_entry = next(
            (e for e in settings.llm_pool if e.id == secondary_llm_id), None
        )
        if (
            settings.spec_driven_v2_verify_enabled
            and _verify_entry is not None
            and _verify_entry.provider in _STRUCTURED_PROVIDERS
        ):
            try:
                from app.adapters.slot_verifier_llm import SlotVerifierLlm
                from app.adapters.tools.retrieval_verify_slot import RetrievalVerifySlotTool

                # verify(slot) 프롬프트 source(registry 호스팅, sha 핀) — tool 이전에
                # 구성해야 하므로 여기서 만든다. AgentDeps 로도 같은 인스턴스를 넘겨 재현 핀
                # 일관. 슬롯 1개의 청크 전체를 한 프롬프트로 합쳐 단일 호출(verify_slot_v2).
                _verify_source = SpecDrivenVerifySource(Path(settings.prompt_local_dir))
                verify_slot_tool = RetrievalVerifySlotTool(
                    slot_verifier=SlotVerifierLlm(
                        llm=secondary_llm, source=_verify_source,
                        # 동시 슬롯 호출 전역 캡(러너 fan-out 전체에 걸침). 별도 튜너블 대신
                        # max_queries 를 재사용한다 — N2 가 띄우는 슬롯×쿼리 규모와 Node2
                        # 검증의 동시 슬롯 규모를 같은 손잡이로 함께 키운다. 단, verify 와
                        # follow_up 이 같은 Node2 vLLM 을 공유하므로 두 도구의 동시 호출이
                        # 같은 max_num_seqs 를 두고 경쟁한다 — KV cache 적체가 보이면
                        # SPEC_DRIVEN_MAX_QUERIES / DOCUMENTS_REF_MAX_CONCURRENCY 로 함께 낮춘다.
                        max_concurrency=settings.spec_driven_max_queries,
                    ),
                    # 동시 슬롯 캡(러너 _verify_sem 과 동일 취지의 tool 레벨 캡).
                    max_concurrency=settings.spec_driven_v2_verify_concurrency,
                )
            except Exception as _exc:
                structlog.get_logger("retrieval.verify_slot.boot").warning(
                    "verify_slot_tool_disabled",
                    error=str(_exc),
                    llm_id=secondary_llm_id,
                    hint="secondary_llm is openai_compat but tool init failed; single-node degrade",
                )

        # retrieval.rescope — verify_slot 이 none_necessary(1차 검색 전체 빗나감)로 판정한
        # 슬롯의 검색 스코프를 재계획한다. verify/follow_up 과 같은 **sub 노드**(Node2 =
        # secondary_llm)에서 돌고, 같은 가드(secondary_llm 이 structured-output provider 일
        # 때만 배선)를 쓴다. 미배선/실패는 graceful skip(none_necessary 슬롯 재검색 없음 →
        # 그 슬롯 기여 0, 단일노드 degrade). verify·follow_up 과 같은 Node2 를 공유하므로 셋의
        # 동시 호출이 같은 vLLM 예산을 두고 경쟁한다(아래 캡 — verify/follow_up 과 동일 손잡이).
        rescope_tool = None
        _rescope_entry = next(
            (e for e in settings.llm_pool if e.id == secondary_llm_id), None
        )
        if (
            settings.spec_driven_v2_verify_enabled
            and _rescope_entry is not None
            and _rescope_entry.provider in _STRUCTURED_PROVIDERS
        ):
            try:
                from app.adapters.rescope_llm import RescopeLlm
                from app.adapters.tools.retrieval_rescope import RetrievalRescopeTool

                _rescope_source = SpecDrivenRescopeSource(Path(settings.prompt_local_dir))
                composer_rescope_source = _rescope_source
                rescope_tool = RetrievalRescopeTool(
                    rescoper=RescopeLlm(
                        llm=secondary_llm, source=_rescope_source,
                        # 동시 슬롯 호출 전역 캡 — verify/follow_up 과 동일 손잡이
                        # (max_queries). 셋이 같은 Node2 vLLM 을 공유하므로 KV cache 적체가
                        # 보이면 SPEC_DRIVEN_MAX_QUERIES / DOCUMENTS_REF_MAX_CONCURRENCY 로 함께 낮춘다.
                        max_concurrency=settings.spec_driven_max_queries,
                    ),
                    # 동시 슬롯 캡(러너 _verify_sem 과 동일 취지의 tool 레벨 캡).
                    max_concurrency=settings.spec_driven_v2_verify_concurrency,
                )
            except Exception as _exc:
                structlog.get_logger("retrieval.rescope.boot").warning(
                    "rescope_tool_disabled",
                    error=str(_exc),
                    llm_id=secondary_llm_id,
                    hint="secondary_llm is openai_compat but tool init failed; no re-scope re-search",
                )

        # 검색·범위·용어 도구. retrieval.search = 내부 retriever 재사용 + Reranker 정렬
        # (실 cross-encoder 는 배포 시 주입, dev/test 는 identity 폴백 — seam 보존).
        # scope=CorpusMap 결정론. 용어 정규화/확장은 용어집(ISO 25964 lookup).
        from app.adapters.reranker.identity import IdentityReranker
        from app.adapters.tools.retrieval_search import RetrievalSearchTool
        from app.adapters.tools.retrieval_scope import RetrievalScopeTool
        from app.adapters.tools.terminology_canonicalize import TerminologyCanonicalizeTool
        from app.adapters.tools.terminology_expand import TerminologyExpandTool

        tools = {
            "retriever.search": retriever_tool,
            "retrieval.search": RetrievalSearchTool(
                retriever=retriever_tool, reranker=IdentityReranker(),
                # 후보 풀 깊이를 최종 top_k 와 분리 — reranker 가 깊은 풀에서 상위
                # top_k 를 고르게 한다(retrieval_fetch_k).
                fetch_k=settings.retrieval_fetch_k,
            ),
            "retrieval.scope": RetrievalScopeTool(
                corpus_map=corpus_map,
                tau_high=settings.retrieval_scope_tau_high,
                tau_low=settings.retrieval_scope_tau_low,
                min_token_count=settings.retriever_min_token_count,
            ),
            "terminology.canonicalize": TerminologyCanonicalizeTool(vocab=terminology_vocab),
            "terminology.expand": TerminologyExpandTool(vocab=terminology_vocab),
            # 검색 후 reranker — opensearch 경로는 SPLADE sparse 모델 기반
            # (query×doc 희소 벡터 내적), local 경로는 결정론 lexical fake. 둘 다 동일
            # retriever.rerank 도구 계약이라 dispatcher 무변경.
            "retriever.rerank": reranker_tool,
            "document.resolve_citation": document_tool,
            "document.fetch_section": fetch_section_tool,
            "document.fetch_chunks": fetch_chunks_tool,
            "memory.session_load": SessionLoadTool(session_store),
            "memory.session_update": SessionUpdateTool(
                session_store, ttl_days=settings.memory_session_ttl_days
            ),
            "memory.approved_search": ApprovedSearchStubTool(),
            "verification.citation_check": LocalCitationCheckTool(),
            "verification.faithfulness_check": LocalFaithfulnessCheckTool(),
        }
        if follow_up_tool is not None:
            tools["retrieval.follow_up"] = follow_up_tool
        if verify_slot_tool is not None:
            tools["retrieval.verify_slot"] = verify_slot_tool
        if rescope_tool is not None:
            tools["retrieval.rescope"] = rescope_tool
        tool_executor = ToolExecutor(registry=registry, tools=tools, event_sink=event_sink)

        # 분류 프롬프트 source(registry 호스팅) — boot 시 fragment sha 검증(fail-fast).
        # llm/hybrid classifier backend 가 공유한다(인라인 _PROMPT 대체).
        classification_prompt_source = ClassificationPromptSource(
            Path(settings.prompt_local_dir)
        )
        # spec_driven_v1 N1/N2/N4 프롬프트 source(registry 호스팅) — 동일 fail-fast sha
        # 검증. N1/N2 는 json_schema guided(output_schema 동반), N4 는 자유 텍스트.
        spec_driven_answer_spec_source = SpecDrivenAnswerSpecSource(
            Path(settings.prompt_local_dir)
        )
        spec_driven_query_source = SpecDrivenQuerySource(
            Path(settings.prompt_local_dir)
        )
        spec_driven_generation_source = SpecDrivenGenerationSource(
            Path(settings.prompt_local_dir)
        )
        # N0 Triage(라우팅 판정, json_schema guided) + N4-G General Generation(자유 텍스트).
        spec_driven_triage_source = SpecDrivenTriageSource(
            Path(settings.prompt_local_dir)
        )
        spec_driven_general_source = SpecDrivenGeneralSource(
            Path(settings.prompt_local_dir)
        )
        # composer N4 슬롯 파이프라인 프롬프트(슬롯 생성 / 종합 / L1 검수) — 동일 fail-fast
        # sha 검증. spec_driven_v1 와 무관(별 variant), composer 활성 시에만 의미.
        composer_slot_source = ComposerSlotSource(Path(settings.prompt_local_dir))
        composer_synthesize_source = ComposerSynthesizeSource(
            Path(settings.prompt_local_dir)
        )
        composer_slot_verify_source = ComposerSlotVerifySource(Path(settings.prompt_local_dir)
        )
        # composer v2 — 책임 재분배(split.design.v1): N1 답변설계(검색지식 제거)+v2 스키마 /
        # N2 검색설계(address map 흡수) / 슬롯 role 소비판. composer variant 만 쓴다 —
        # spec_driven_v1/v2 의 N1/N2 source 는 불변(A/B 비교). 미배선이면 composer 는 계승한
        # base source(v1)로 graceful fallback(점진 도입 — _build_composer 가 v2 우선).
        composer_answer_spec_source = ComposerAnswerSpecSource(
            Path(settings.prompt_local_dir)
        )
        composer_query_source = ComposerQuerySource(Path(settings.prompt_local_dir))
        composer_slot_v2_source = ComposerSlotV2Source(Path(settings.prompt_local_dir))
        # composer 다중 페르소나(persona_framework.design.v1 §10) — persona_id → profile
        # fragment. 페르소나 variant(composer_reviewer 등)가 자기 id 로 조회(단일 fragment,
        # N1/N2/N4 가 공유). 중립 `composer`(persona=None)는 무관.
        composer_persona_sources = {
            pid: ComposerPersonaSource(
                Path(settings.prompt_local_dir),
                profile_id=f"composer_persona_{pid}_v1",
            )
            for pid in ("reviewer", "designer", "operator")
        }
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
        spec_driven_answer_spec_source=spec_driven_answer_spec_source,
        spec_driven_query_source=spec_driven_query_source,
        spec_driven_generation_source=spec_driven_generation_source,
        spec_driven_triage_source=spec_driven_triage_source,
        spec_driven_general_source=spec_driven_general_source,
        composer_slot_source=composer_slot_source,
        composer_synthesize_source=composer_synthesize_source,
        composer_slot_verify_source=composer_slot_verify_source,
        composer_answer_spec_source=composer_answer_spec_source,
        composer_query_source=composer_query_source,
        composer_slot_v2_source=composer_slot_v2_source,
        composer_persona_sources=composer_persona_sources,
        composer_rescope_source=composer_rescope_source,
        secondary_llm=secondary_llm,
        summarizer=summarizer,
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
            # spec_driven_v1 — N2 per-slot 멀티쿼리 상한 + N3 1차 floor 정렬 budget.
            # 명시 필드(settings.py)라 SPEC_DRIVEN_* env 가 동작한다(getattr 폴백 제거).
            "spec_driven_max_queries": settings.spec_driven_max_queries,
            "spec_driven_max_context_chunks": settings.spec_driven_max_context_chunks,
            # N4 생성 컨텍스트 토큰 예산(0=무제한). 1차 전량 보존 + 2차 score 순 채움 캡.
            "spec_driven_context_token_budget": settings.spec_driven_context_token_budget,
            # spec_driven_v2 — Node2 검증 토글 + 동시 검증 슬롯 상한(per-slot 파이프라인 캡).
            "spec_driven_v2_verify_enabled": settings.spec_driven_v2_verify_enabled,
            "spec_driven_v2_verify_concurrency": settings.spec_driven_v2_verify_concurrency,
            # spec_driven_v1 N4 — 인용 계약(파일 호스팅 → rendered_prompt_hash 에 반영).
            "citation_contract_path": str(
                Path(settings.prompt_local_dir) / "system" / "citation_contract_v1.md"
            ),
            # composer variant N4 슬롯 파이프라인(COMPOSER_* env → settings → tunable).
            "composer_slot_verify": settings.composer_slot_verify,
            "composer_slot_max_tokens": settings.composer_slot_max_tokens,
            "composer_synthesize": settings.composer_synthesize,
            "composer_slot_context_k": settings.composer_slot_context_k,
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
        utility_llm=utility_llm_id,  # 폴백 해석된 실제 id(빈 값이면 default_llm).
        secondary_llm=secondary_llm_id,  # Node2 외부참조 선별(follow_up) LLM(빈 값이면 default_llm).
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
