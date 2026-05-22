SHELL := /bin/sh

COMPOSE := docker compose --env-file infra/env/local.env --profile local \
  -f infra/compose/compose.yml -f infra/compose/compose.local.yml

COMPOSE_ONPREM := docker compose --env-file infra/env/onprem.env --profile onprem \
  -f infra/compose/compose.yml -f infra/compose/compose.onprem.yml

.PHONY: help build up-local down logs ps test test-integration smoke smoke-stream seed seed-encode opensearch-init verify-w1 fmt clean migrate psql prompts-validate \
  build-onprem up-onprem down-onprem logs-onprem ps-onprem clean-onprem _guard-local-only

help:
	@echo "Targets:"
	@echo "  build      Build backend image"
	@echo "  up-local   Bring up local profile stack"
	@echo "  down       Tear down stack (keeps volumes)"
	@echo "  logs       Tail agent-api logs"
	@echo "  ps         Show stack status"
	@echo "  test       Run backend unit tests"
	@echo "  smoke      POST a sample query to /v1/chat/completions"
	@echo "  smoke-stream  Stream a sample query (SSE) and pretty-print each frame"
	@echo "  fmt        Run ruff format on backend"
	@echo "  clean      Tear down stack and remove volumes"
	@echo ""
	@echo "On-premise (air-gapped + local vLLM):"
	@echo "  build-onprem  Build agent-api/open-webui images for onprem profile"
	@echo "  up-onprem     Bring up onprem stack (requires vllm-node:latest + ./models/gemma4)"
	@echo "  down-onprem   Tear down onprem stack (keeps volumes)"
	@echo "  logs-onprem   Tail agent-api logs (onprem)"
	@echo "  ps-onprem     Show onprem stack status"
	@echo "  clean-onprem  Tear down onprem stack and remove volumes"

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

smoke-stream:
	./scripts/smoke-stream.sh

# 인덱스 (재)생성 / 시드 타깃은 모두 `--recreate` 경로를 통과하므로 local 개발
# 환경에서만 실행해야 한다. onprem 스택은 사전에 적재된 opensearch_data 볼륨을
# 그대로 사용한다는 전제이며, 시드/초기화는 자동으로 일어나지 않는다.
#
# _guard-local-only: onprem 컨테이너(`agent-backend-vllm`)가 떠 있으면 차단.
# (호스트의 9200 포트가 onprem opensearch 로 매핑되어 있을 위험을 회피)
_guard-local-only:
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -qE '^agent-backend-vllm$$'; then \
	  echo >&2 "[guard] onprem 스택이 실행 중입니다 (agent-backend-vllm 감지)."; \
	  echo >&2 "        seed / opensearch-init 타깃은 local 전용입니다. 먼저 'make down-onprem' 후 재시도하세요."; \
	  exit 1; \
	fi

opensearch-init: _guard-local-only
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	OPENSEARCH_INDEX=nrc-all-v3 \
	OPENSEARCH_SEARCH_PIPELINE=nrc-hybrid-search \
	sh infra/opensearch/init.sh

# BM25-only seed (no embeddings). Use `make seed-encode` for full hybrid.
# Local profile 전용 — onprem 스택의 색인을 손대지 않는다.
seed: _guard-local-only
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	OPENSEARCH_INDEX=nrc-all-v3 \
	SEED_FILE=datasets/seed_docs/smr_seed.jsonl \
	python3 scripts/seed_opensearch.py --recreate

# Full hybrid seed — runs inside the **local** agent-api container so the host
# doesn't need torch. Requires `make build` to have included the [embeddings]
# extra. Models download to /var/cache/huggingface (hf_cache volume) on first run.
# Local profile 전용 — $(COMPOSE) 가 local profile 에 고정되어 있어 onprem
# 컨테이너에는 닿지 않으며, 추가로 _guard-local-only 가 호스트 9200 보호.
seed-encode: _guard-local-only
	$(COMPOSE) exec -T \
	  -e OPENSEARCH_ENDPOINT=http://opensearch:9200 \
	  -e OPENSEARCH_INDEX=nrc-all-v3 \
	  -e SEED_FILE=/app/datasets/seed_docs/smr_seed.jsonl \
	  -e OPENSEARCH_MAPPING_FILE=/app/infra/opensearch/mappings/nrc-all-v3.json \
	  agent-api python /app/scripts/seed_opensearch.py --recreate --encode

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

# ── On-premise targets ────────────────────────────────────────────────────
# 사전 준비:
#   1) docker/vllm/README.md 절차로 호스트 docker 데몬에 `vllm-node:latest` 적재
#   2) ./models/gemma4 에 Gemma 4 26B A4B-it 가중치 사전 다운로드
#   3) (선택) hf_cache 볼륨에 임베더(e5/fermi) 사전 동기화 (HF_HUB_OFFLINE=1)
build-onprem:
	$(COMPOSE_ONPREM) build agent-api open-webui

up-onprem:
	$(COMPOSE_ONPREM) up -d

down-onprem:
	$(COMPOSE_ONPREM) down

logs-onprem:
	$(COMPOSE_ONPREM) logs -f agent-api

ps-onprem:
	$(COMPOSE_ONPREM) ps

clean-onprem:
	$(COMPOSE_ONPREM) down -v
