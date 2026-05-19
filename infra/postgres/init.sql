-- v2 §17 — State DB schema. Phase 3 uses only session_memory; remaining tables
-- are created up-front for forward compatibility.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS session_memory (
    session_id TEXT PRIMARY KEY,
    user_id TEXT,
    project_id TEXT,
    active_entities JSONB NOT NULL DEFAULT '{}',
    active_scenario_object TEXT,
    active_scenario_depth TEXT,
    conversation_summary TEXT,
    recent_turns JSONB NOT NULL DEFAULT '[]',
    last_retrieved_chunk_ids JSONB NOT NULL DEFAULT '[]',
    last_memory_ids_used JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_session_memory_expires ON session_memory (expires_at);

CREATE TABLE IF NOT EXISTS memory_candidates (
    memory_id TEXT PRIMARY KEY,
    source_interaction_id TEXT NOT NULL,
    source_trace_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scenario_object TEXT,
    scenario_depth TEXT,
    entities JSONB NOT NULL DEFAULT '{}',
    claim TEXT,
    answer_summary TEXT,
    supporting_chunk_ids JSONB NOT NULL DEFAULT '[]',
    citations JSONB NOT NULL DEFAULT '[]',
    verification_status TEXT,
    expert_review_status TEXT NOT NULL DEFAULT 'candidate',
    staleness_status TEXT NOT NULL DEFAULT 'unknown',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approved_memories (
    memory_id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    namespace TEXT NOT NULL,
    scenario_object TEXT,
    scenario_depth TEXT,
    entities JSONB NOT NULL DEFAULT '{}',
    canonical_question TEXT,
    canonical_answer TEXT,
    claim TEXT,
    supporting_chunk_ids JSONB NOT NULL DEFAULT '[]',
    citations JSONB NOT NULL DEFAULT '[]',
    source_document_revisions JSONB NOT NULL DEFAULT '[]',
    embedding vector,
    version INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'approved',
    approved_by TEXT NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tool_call_records (
    tool_call_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    error_code TEXT,
    latency_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_interaction ON tool_call_records (interaction_id);

CREATE TABLE IF NOT EXISTS expert_reviews (
    review_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dataset_candidates (
    dataset_candidate_id TEXT PRIMARY KEY,
    source_interaction_id TEXT NOT NULL,
    source_trace_id TEXT NOT NULL,
    failure_type TEXT,
    scenario_object TEXT,
    scenario_depth TEXT,
    question TEXT NOT NULL,
    expected_answer TEXT,
    expected_citations JSONB NOT NULL DEFAULT '[]',
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
