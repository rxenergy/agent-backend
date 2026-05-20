# infra/postgres/migrations

Reserved for forward schema migrations once the State DB has live data
that cannot be rebuilt from `init.sql`.

## Current policy (Phase 3)

Schema is managed in-place via `init.sql`, executed by Postgres at first
boot from `/docker-entrypoint-initdb.d`. The 6 tables in spec v2 §17 are
all created up-front — no Alembic, no Flyway, no online migrations.

## When to switch

Introduce a migration tool (Alembic recommended) when **either** holds:

- Phase 4+ ships and `memory_candidates` / `expert_reviews` carry data
  that must survive a schema change.
- aws-mvp profile is brought up against a long-lived RDS instance.

Record the decision as an ADR under `docs/plans/adr/` before adding the
tool — do not introduce migrations silently.
