# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Source of Truth

The authoritative architecture document is `docs/plans/agent_expreiment_platform_architecture.mvp.v2.md` (Korean). The earlier `mvp.md` revision is no longer the source of truth. It defines an **Agent experimentation platform** for an SMR (Small Modular Reactor) licensing / nuclear regulation domain QA Agent. Do not invent commands, layouts, or decisions that aren't grounded there.

## What This Repo Is

A development + experiment environment for an **Agent**, not a SaaS service. The platform's job is to answer this question for any past Agent run: *"can this execution be reproduced?"* — via `interaction_id`, `trace_id`, `rendered_prompt_hash`, `context_hash`.

Three deployment profiles share the same Docker image:
- `local` — developer machine, MinIO artifact store
- `aws-mvp` — EC2 + Docker Compose + S3 (single VM, no managed services beyond S3)
- `onprem` — VM/bare metal + Docker Compose + MinIO, no external network

Profile differences are env vars only — never branch the image.

## Architecture Principles (Non-Negotiable)

1. **Agent workflow = `AgentRunner` variants.** The API / domain / adapter code does not change when you swap workflows. `AGENT_VARIANT` env var selects: `fake_echo_v0` (P0 test) | `sequential_tool_routed_v2` (default, 15-step via ToolExecutor).
2. **Tools are controlled, not LLM-discovered.** Every external capability — retrieval, document resolution, memory access, verification, artifact write — is invoked through `ToolExecutor` against `tools/registry.yaml`. The LLM does not pick tools; the workflow does. Retrieval / document resolution backends are selected via `RETRIEVER_BACKEND` (`opensearch` | `local`) and wired in `backend/app/config/profiles.py`; `tools/registry.yaml` defines policy (timeout / retry / required) only.
3. **Memory is gated.** Session memory injects only on follow-up turns with matching scenario + ≥50% entity overlap. Approved memory (Phase 5) is the only long-term knowledge that reaches the prompt; candidate/stale memory never does.
4. **Hexagonal (Ports & Adapters).** `backend/app/{domain,ports,adapters,application,api,config,observability}`. Domain code MUST NOT import external SDKs.
5. **Reproducibility is a domain rule.** Every response emits an `InteractionEvent` (spec §16) capturing variant, prompt profile/version/hash, context hash, retrieved chunk ids, tool_calls, memory_ids_used, verification result, model options.
6. **Failures are not hidden.** Refusal / clarification / verification failure are first-class outcomes from the vocabulary in `backend/app/domain/errors.py` — never papered over with smooth prose.
7. **One Docker image, three profiles.** Topology differences live in `infra/compose/compose.*.yml` + `infra/env/*.env`, not in the image.
8. **OpenAI-compatible boundary.** Backend exposes `/v1/chat/completions` and `/v1/models`. Non-OpenAI data (citations, refusal reason, scenario metadata) ride in custom response fields and/or separate endpoints. Session continuity uses `X-Session-Id` / `X-User-Id` / `X-Project-Id` headers.

## Tech Stack (Locked for Phase 0–2)

- **Backend:** Python 3.11+, FastAPI + uvicorn, Pydantic v2, structlog (JSON), httpx, asyncpg, tenacity
- **Observability:** OpenTelemetry SDK → OTel Collector → Phoenix + Tempo + Prometheus + Loki + Grafana
- **State DB:** PostgreSQL 16 + pgvector (Phase 3+). Session memory live; candidate / approved / tool_call_records / expert_reviews / dataset_candidates tables provisioned ahead of Phase 4–6 use.
- **Agent workflow (Phase 0–3):** sequential 15-step via `ToolExecutor`. LangGraph is a future variant.
- **Prompt management:** local Git registry (`prompts/`). Phoenix prompt management is a Phase 6+ option.
- **Tool registry:** `tools/registry.yaml`. All capabilities (retriever / document / memory / verification / artifact) invoked through `ToolExecutor` with timeout + retry + span.
- **Artifact store:** MinIO (local/onprem) → S3 (aws-mvp, Phase 7+).
- **LLM / Retriever (Phase 0–3):** fake adapters only. Real providers arrive in later phases.

## Repository Layout

```
backend/                 # FastAPI service (hexagonal)
  app/
    api/                 # /health, /v1/chat/completions, /v1/models
    application/
      agents/            # fake_echo_v0, sequential_tool_routed_v2
      tools/             # registry, executor, policy, errors
      memory/            # session policies, resolver
      prompting/         # resolver, renderer
      context/           # ContextPack, ContextBuilder
      events/            # EventRecorder
    domain/              # interaction, scenario, errors, tools, memory, retrieval
    ports/               # llm, tool, event_sink, memory_store, vector_store
    adapters/
      tools/             # retriever_local, document_local, verification_local, memory_session_local, memory_approved_stub, artifact_event
      postgres/          # asyncpg pool + session memory store
      event_sink_filesystem, event_sink_minio
      in_memory_session_store, llm_fake
    observability/       # otel.py, logging.py
    config/              # settings.py, profiles.py (adapter factory)
  tests/unit/
  Dockerfile
  pyproject.toml

prompts/                 # registry.yaml + fragments (system/, object/, depth/, cell/, schemas/)
tools/                   # registry.yaml + per-tool schemas

infra/
  compose/               # compose.yml + per-profile overlays
  env/                   # per-profile .env files
  postgres/              # init.sql (v2 §17 — 6 tables), migrations/
  otel/ tempo/ prometheus/ loki/ grafana/ minio/ qdrant/

scripts/                 # run-local.sh, smoke.sh
Makefile                 # build, up-local, down, test, smoke, migrate, psql
```

## Build / Test / Run

- `make build` — build backend image
- `make up-local` — bring up local profile (backend + OTel collector + Phoenix + Grafana stack + MinIO)
- `make down` — tear down
- `make smoke` — POST a sample query to `/v1/chat/completions`
- `make test` — backend unit tests (no containers required)

Unit tests do NOT depend on adapters or containers — they exercise domain + application using fake ports directly.

## Phase Roadmap (spec §18)

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | env skeleton, fake_echo_v0, single trace | done |
| 1 | node-level spans, InteractionEvent | done (absorbed into v2 variant) |
| 2 | PromptResolver/Renderer, ContextPack, MinIO artifact sink | done |
| 2.5 | Tool Registry/Executor, 15-step workflow, tool_calls in event | done |
| 3 | Postgres + pgvector, session memory store, multi-turn | done |
| 3.1 | OpenSearch retrieval interface (Tool port + preflight + error mapping; query/mapping deferred) | done |
| 3.5 | Verification tool strengthening (Ragas) | future |
| 4 | Memory candidate / Expert review | future |
| 5 | Approved memory + pgvector ANN | future |
| 6 | Eval Runner (Ragas/Promptfoo/Phoenix experiments) | future |
| 7 | aws-mvp profile (EC2 + S3) | future |
| 8 | onprem profile (image export/import, full local mode) | future |

## When Working in This Repo

- The spec is in Korean with English technical terms. Match that style in code comments and docs.
- Resist over-engineering. The platform's center of gravity is `AgentRunner`, `PromptResolver`, `ContextPack`, OTel traces, and the artifact store — not model serving, not OpenSearch, not Kubernetes.
- `compose.cloud.yml`, `deploy/cloud/`, and the v1 spec md were removed as legacy. The v2 spec at `docs/plans/agent_expreiment_platform_architecture.mvp.v2.md` is the single source. No backward-compat shims are kept.
- Brand identity assets in `frontend/branding/` (RX monochrome palette) are preserved for future OpenWebUI client use; not required by Phase 0–2.
