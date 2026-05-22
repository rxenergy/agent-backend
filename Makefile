SHELL := /bin/sh

COMPOSE := docker compose --env-file infra/env/local.env --profile local \
  -f infra/compose/compose.yml -f infra/compose/compose.local.yml

.PHONY: help build up-local down logs ps test test-integration smoke seed seed-encode opensearch-init verify-w1 fmt clean migrate psql prompts-validate

help:
	@echo "Targets:"
	@echo "  build      Build backend image"
	@echo "  up-local   Bring up local profile stack"
	@echo "  down       Tear down stack (keeps volumes)"
	@echo "  logs       Tail agent-api logs"
	@echo "  ps         Show stack status"
	@echo "  test       Run backend unit tests"
	@echo "  smoke      POST a sample query to /v1/chat/completions"
	@echo "  fmt        Run ruff format on backend"
	@echo "  clean      Tear down stack and remove volumes"

build:
	$(COMPOSE) build agent-api open-webui

build-frontend:
	$(COMPOSE) build open-webui

up-local:
	./scripts/run-local.sh

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f agent-api

ps:
	$(COMPOSE) ps

test:
	cd backend && python -m pytest -q -m "not integration"

# Live integration tests against a running OpenSearch (and optional Anthropic API).
# Requires `make up-local` (for OpenSearch on localhost:9200) and, for the
# anthropic_live marker, ANTHROPIC_API_KEY in the environment. Tests are skipped
# automatically when their required env vars are absent.
test-integration:
	cd backend && OPENSEARCH_TEST_ENDPOINT=$${OPENSEARCH_TEST_ENDPOINT:-http://localhost:9200} \
	  python -m pytest -q -m integration tests/integration

prompts-validate:
	python3 scripts/validate_prompts.py prompts

smoke:
	./scripts/smoke.sh

opensearch-init:
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	OPENSEARCH_INDEX=nrc-all-v3 \
	OPENSEARCH_SEARCH_PIPELINE=nrc-hybrid-search \
	sh infra/opensearch/init.sh

# BM25-only seed (no embeddings). Use `make seed-encode` for full hybrid.
seed:
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	OPENSEARCH_INDEX=nrc-all-v3 \
	SEED_FILE=datasets/seed_docs/smr_seed.jsonl \
	python3 scripts/seed_opensearch.py --recreate

# Full hybrid seed: requires `pip install -e backend/[embeddings]` on the host.
seed-encode:
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	OPENSEARCH_INDEX=nrc-all-v3 \
	SEED_FILE=datasets/seed_docs/smr_seed.jsonl \
	python3 scripts/seed_opensearch.py --recreate --encode

verify-w1:
	./scripts/verify-w1.sh

fmt:
	cd backend && ruff format app tests && ruff check --fix app tests

clean:
	$(COMPOSE) down -v

migrate:
	$(COMPOSE) exec -T postgres psql -U agent -d agent_state -f /docker-entrypoint-initdb.d/init.sql

psql:
	$(COMPOSE) exec postgres psql -U agent -d agent_state
