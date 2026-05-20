# infra/qdrant

Reserved for Qdrant configuration when the `memory-scale` compose profile
is activated.

## Activation

Qdrant is defined in `infra/compose/compose.yml` under
`profiles: ["memory-scale"]` and is **not** brought up by the default
local / aws-mvp / onprem profiles.

Switch to it only when approved-memory vector search outgrows pgvector
(spec v2 §6, §20). Until then this directory holds nothing but this
README — config files (collection definitions, snapshot policy) land
here when the migration happens.
