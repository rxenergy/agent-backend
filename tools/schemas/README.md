# tools/schemas

Per-tool input/output JSON Schemas referenced by `tools/registry.yaml`.

Naming: `<tool_name>.input.json`, `<tool_name>.output.json`.
Example: `retriever.search.input.json`, `retriever.search.output.json`.

Scope: tool I/O contracts only. LLM response schemas live in `prompts/schemas/`.

Status (Phase 2.5): schemas are not yet enforced at the executor boundary —
this directory exists to anchor the contract location ahead of Phase 3.5
verification strengthening (spec v2 §8.5).
