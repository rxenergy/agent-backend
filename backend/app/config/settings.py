from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMPoolEntry(BaseModel):
    """One HttpLLM endpoint exposed to the /v1/models dropdown.

    `api_key_env` is the **name** of the env var that holds the key — the value
    itself stays out of settings so secrets aren't logged when settings are dumped.
    """

    id: str
    provider: Literal["openai_compat", "anthropic"]
    endpoint: str
    model: str
    api_key_env: str | None = None
    timeout_s: float = 30.0
    max_attempts: int = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    app_profile: Literal["local", "aws-mvp", "onprem"] = "local"
    environment: str = "development"
    log_level: str = "INFO"

    service_version: str = "0.2.0"

    # Agent variant pool + default selection
    agent_variants_enabled: list[str] = [
        "agentic_finder_v4",
        "hierarchical_corrective_v3_1",
        "react_minimal_v1",
        "react_echo_v1",
        "fake_echo_v0",
    ]
    default_variant: str = "agentic_finder_v4"

    # react_minimal_v1 — ReAct Retrieval 루프 턴 backstop(submit_response 미발동 시 종료).
    react_max_turns: int = 8

    # spec_driven_v1 — N2 per-slot 멀티쿼리 상한 + N3 1차 floor 정렬 budget.
    # max_context_chunks 는 더 이상 최종 cap 이 아니다(최종 크기는 아래 token budget 이
    # 지배) — N3 floor 정렬 단계의 예산일 뿐. 명시 필드여야 SPEC_DRIVEN_* env override 가
    # 동작한다(model_config extra="ignore" 라 getattr 로만 읽으면 미선언 env 가 무시됨).
    # profiles.py 는 이 필드를 직접 읽는다. 컨텍스트 윈도 확장 모델에선 슬롯 세분·다양
    # 쿼리로 근거를 넓게 모은다.
    spec_driven_max_queries: int = 10
    spec_driven_max_context_chunks: int = 24

    # Classifier (Node 1)
    classifier_backend: Literal["rule", "llm", "hybrid"] = "rule"
    classification_threshold: float = 0.35
    classifier_escalate_below: float = 0.35

    # Active cell policy (기획 doc §3 / §7)
    # "all"          — 16 cells active (verification로 품질 게이팅)
    # "top_priority" — 기획 §3 Top Priority 5 셀만 active
    active_cells_mode: Literal["all", "top_priority"] = "all"

    # Verification thresholds (Node 4, 기획 doc §Node4)
    verification_citation_threshold: float = 0.9
    verification_faithfulness_threshold: float = 0.85
    verification_retry_on_fail: bool = True
    # Phase D claim 검증(Node 14 decompose → 15 verify) 토글. streaming 출력에서는
    # 생성 텍스트가 이미 사용자에게 전송된 뒤라 사후 claim 검증이 답변을 되돌릴 수
    # 없으므로 임시 비활성 가능. False 면 verification_status=SKIPPED 로 통과.
    # (Node 6 retrieval_evaluate 게이트와는 무관 — 그쪽은 항상 동작.)
    claim_verification_enabled: bool = True

    # Multi-turn summary (option, 기획 doc §Multi-Turn Context Handling)
    multi_turn_summary_enabled: bool = True
    multi_turn_keep_turns: int = 5

    # LLM pool — JSON list, parsed by pydantic-settings.
    # fake-echo는 항상 풀에 자동 포함되므로 여기 정의하지 않는다.
    llm_pool: list[LLMPoolEntry] = []
    default_llm: str = "fake-echo"
    utility_llm: str = ""
    llm_timeout_s: float = 30.0
    llm_max_attempts: int = 2

    # Retriever backend (W1)
    retriever_backend: Literal["local", "opensearch"] = "local"
    # 최종 결과 개수(operating point). 벤치마크가 가중치를 이 k 에 맞춰 튜닝하므로
    # OpenSearch hybrid pipeline 선택도 이 값에 연동된다(profiles.py):
    #   k=10 → nrc-hybrid-search-k10 (weights=[0.4, 0.2, 0.4])
    #   그 외(5 포함) → opensearch_search_pipeline 폴백
    # (k=5 전용 pipeline 은 클러스터 미등록으로 맵에서 제외 — profiles.py 참조)
    retriever_top_k: int = 3
    # v3.1 Node 5 — 전략별 후보 풀 fetch 깊이 (spec ~20). 최종 top_k 와 분리:
    # 깊게 fetch 해 RRF 융합해야 더 나은 상위 top_k 를 고른다.
    retrieval_fetch_k: int = 20
    # v3.1 Node 7 — WEAK/FAIL 시 결정론 복구 최대 라운드(루프 종료 보장).
    retrieval_max_recover_rounds: int = 2
    retriever_min_score: float = 0.0
    retriever_k_dense: int = 50
    # v3.1 Layer 1 범위 한정(corpus_map) — confidence 게이트 임계.
    #   conf >= tau_high → hard filter / tau_low <= conf < tau_high → boost /
    #   conf < tau_low → off. 잘못된 hard scope 의 recall 절벽을 confidence 로 방어.
    retrieval_scope_tau_high: float = 0.6
    retrieval_scope_tau_low: float = 0.3
    # v3.1 Layer 2 노이즈 floor 기본값 — corpus_map.chunk_quality 가 우선,
    # 맵 미배치 시에만 이 값 적용(0=비활성).
    retriever_min_token_count: int = 10
    # v3.1 P1 Section auto-merge — 승격 chunk 의 형제 fetch 상한(섹션 폭주 방지).
    section_merge_max_chunks: int = 50
    # v3.1 P1b 예산 거버너 — 컨텍스트 Σ token_count 캡. **기본 0=off(opt-in)**:
    # 모델 context window 가 프로파일마다 다르고, 잘못된 캡이 규제 근거를 과도
    # 축소(false refusal)할 위험이 있어, 운영자가 프로파일별로 확인 후 켠다.
    # 0 이면 거버너(drop/demote/재배치) 전체 비활성 → 기존 동작과 byte-identical.
    context_token_budget: int = 0
    # spec_driven_v1 튜너블 — N4 생성 컨텍스트 Σ(추정 토큰) 캡.
    # 0=무제한(기존 동작). onprem 은 9000 으로
    # vLLM 16384 윈도우 안전 — 1차 검색 전량 보존 + 2차 검색을 score 순으로 이 예산
    # 한도까지 채운다(spec_driven_v1._assemble_final_chunks).
    spec_driven_context_token_budget: int = 0
    opensearch_endpoint: str = "http://opensearch:9200"
    opensearch_index: str = "nrc-all-v1"
    # 적재 데이터가 따르는 인덱스 스키마 버전의 *선언적* 단일 출처.
    # 인덱스 이름과 분리한다 — 이름은 임의(`nrc-smr-2026` 등)일 수 있으므로
    # 능력 판단을 네이밍 관습에 의존하지 않는다. v3.1 G3 규제 신호
    # (clause_id/jurisdiction/effective_on)는 v2 에서만 신뢰 가능하며,
    # v1 에서는 authority_tier(collection 유도)만 부분 동작한다. 운영자가
    # 코퍼스를 v2 스키마로 재적재한 뒤 이 값을 "v2" 로 올린다.
    opensearch_schema_version: Literal["v1", "v2"] = "v1"
    # hybrid pipeline 폴백. retriever_top_k 가 벤치마크 operating point(현재 10)면
    # profiles.py 가 k-전용 pipeline 으로 오버라이드하고, 그 외 k(5 포함)는 이 값을 쓴다.
    opensearch_search_pipeline: str = "nrc-hybrid-search"
    opensearch_dense_field: str = "dense_e5"
    opensearch_sparse_field: str = "sparse_fermi"
    opensearch_text_field: str = "text"
    opensearch_username: str = ""
    opensearch_password: str = ""
    opensearch_verify_certs: bool = False

    # Embedding models (hybrid retrieval; loaded only when retriever_backend=opensearch)
    embedding_e5_model: str = "intfloat/multilingual-e5-large"
    embedding_fermi_model: str = "atomic-canyon/fermi-1024"
    embedding_device: str = "cpu"
    embedding_e5_max_seq_len: int = 512
    embedding_fermi_max_seq_len: int = 1024
    embedding_fermi_top_n: int = 200

    # Observability
    otel_enabled: bool = True
    otel_service_name: str = "smr-agent-backend"
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"

    phoenix_enabled: bool = False
    phoenix_endpoint: str = "http://phoenix:6006"

    # Prompt management
    prompt_source: Literal["local", "phoenix", "hybrid"] = "local"
    prompt_label: str = "mvp"
    prompt_local_dir: str = "/app/prompts"

    # Tool registry
    tool_registry_path: str = "/app/tools/registry.yaml"

    # Variant registry (ADR-0006)
    variant_registry_path: str = "/app/variants/registry.yaml"

    # Preflight policy (ADR-0007).
    # `warn` — log warnings and continue (default for `local`).
    # `strict` — abort container boot on any failure (default for `aws-mvp`/`onprem`).
    # When unset, `build_container` derives a default from `app_profile`.
    preflight_mode: Literal["warn", "strict", "auto"] = "auto"
    tool_trace_enabled: bool = True
    tool_timeout_default_ms: int = 3000
    tool_retry_default: int = 0

    # Event / artifact sink
    event_sink: Literal["filesystem", "minio", "s3"] = "minio"
    event_bucket: str = "smr-agent-events"
    event_prefix: str = "mvp"
    event_filesystem_root: str = "/var/lib/agent/events"

    minio_endpoint: str = "http://minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    aws_region: str = "ap-northeast-2"

    # OpenAI-compatible thinking surface (workflow → reasoning_content / <think>)
    thinking_expose: bool = True
    # Narration tier for the thinking surface (thinking_renderer):
    #   "summary"  — user-meaningful, Korean, outcome-conditioned (default)
    #   "detailed" — legacy per-node English narration (dev/debug)
    #   "off"      — no workflow narration (LLM reasoning still passes through)
    thinking_verbosity: Literal["summary", "detailed", "off"] = "summary"
    # Per-step preview cap (top-N chunks / hits / citations shown in thinking).
    thinking_max_items: int = 3

    # Capture modes (§13.2, §14)
    trace_content_mode: Literal["metadata", "snippets", "full"] = "metadata"
    context_capture_mode: Literal["metadata", "snippets", "full"] = "metadata"

    # State DB / Memory
    state_db_url: str = "postgresql://agent:agent@postgres:5432/agent_state"
    memory_enabled: bool = True
    memory_store: Literal["postgres", "in_memory"] = "postgres"
    memory_vector_backend: Literal["pgvector", "qdrant"] = "pgvector"
    memory_review_required: bool = True
    memory_approved_only: bool = True
    memory_session_ttl_days: int = 90
    memory_staleness_check_enabled: bool = True


def get_settings() -> Settings:
    return Settings()
