from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    app_profile: Literal["local", "aws-mvp", "onprem"] = "local"
    environment: str = "development"
    log_level: str = "INFO"

    agent_variant: str = "sequential_tool_routed_v2"
    exposed_model_id: str = "agent-search-v1"
    service_version: str = "0.2.0"

    # Classifier (Node 1)
    classifier_backend: Literal["rule", "llm", "hybrid"] = "rule"
    classification_threshold: float = 0.35
    classifier_escalate_below: float = 0.35

    # Verification thresholds (Node 4, 기획 doc §Node4)
    verification_citation_threshold: float = 0.9
    verification_faithfulness_threshold: float = 0.85
    verification_retry_on_fail: bool = True

    # Multi-turn summary (option, 기획 doc §Multi-Turn Context Handling)
    multi_turn_summary_enabled: bool = True
    multi_turn_keep_turns: int = 5

    # LLM provider (W1)
    # provider: fake | openai_compat (vLLM, OpenAI, LM Studio, Ollama) | anthropic
    llm_provider: Literal["fake", "openai_compat", "anthropic"] = "fake"
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_s: float = 30.0
    llm_max_attempts: int = 2

    # Retriever backend (W1)
    retriever_backend: Literal["local", "opensearch"] = "local"
    opensearch_endpoint: str = "http://opensearch:9200"
    opensearch_index: str = "smr-docs"
    opensearch_username: str = ""
    opensearch_password: str = ""
    opensearch_verify_certs: bool = False

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
