-- Provisioned auxiliary databases on the shared Postgres instance.
-- Runs before init.sql by alphabetical order in /docker-entrypoint-initdb.d.
-- Keeps OpenWebUI state isolated from the agent_state schema so the agent
-- pipeline and the UI shell can evolve independently.

CREATE DATABASE openwebui OWNER agent;

-- Phoenix observability store. Replaces the previous file-based SQLite backend
-- to remove the GraphQL/UI rendering bottleneck at scale (concurrent read/write
-- + indexing). Phoenix auto-creates its schema (Alembic) on first start.
CREATE DATABASE phoenix OWNER agent;
