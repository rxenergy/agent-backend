SHELL := /bin/sh

COMPOSE := docker compose --env-file infra/env/local.env --profile local \
  -f infra/compose/compose.yml -f infra/compose/compose.local.yml

.PHONY: help build up-local down logs ps test smoke seed verify-w1 fmt clean migrate psql

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
	cd backend && python -m pytest -q

smoke:
	./scripts/smoke.sh

seed:
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	OPENSEARCH_INDEX=smr-docs \
	SEED_FILE=datasets/seed_docs/smr_seed.jsonl \
	python3 scripts/seed_opensearch.py

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
