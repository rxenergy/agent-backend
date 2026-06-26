SHELL := /bin/sh

COMPOSE := docker compose --env-file infra/env/local.env --profile local \
  -f infra/compose/compose.yml -f infra/compose/compose.local.yml

COMPOSE_ONPREM := docker compose --env-file infra/env/onprem.env --profile onprem \
  -f infra/compose/compose.yml -f infra/compose/compose.onprem.yml

# onprem 변형 — second 노드를 AWS Bedrock Haiku 로 두는 구성(서브 vLLM 미연결).
# compose 토폴로지는 메인 스택과 동일(서브 vLLM 없음)하고 env 만 onprem.bedrock-sub.env.
# 단일 호스트 기동(원격 서브 데몬 불필요 — relevance/multihop 이 Bedrock 으로 나간다).
# ⚠ Bedrock 아웃바운드 필요(완전 air-gapped 아님) + AWS_BEARER_TOKEN_BEDROCK 환경 주입.
#
# ONPREM_ENV_FILE 를 export 해 compose.onprem.yml 의 agent-api env_file 이 이 파일을
# 컨테이너 런타임 env 로 *실제* 로 읽게 한다(--env-file 플래그는 보간 전용이라 컨테이너 env
# 엔 안 들어간다 — 이게 없으면 컨테이너가 기본 onprem.env 를 읽어 bedrock 설정이 무시됨).
COMPOSE_ONPREM_BEDROCK := ONPREM_ENV_FILE=onprem.bedrock-sub.env \
  docker compose --env-file infra/env/onprem.bedrock-sub.env \
  --profile onprem -f infra/compose/compose.yml -f infra/compose/compose.onprem.yml

# 서브 노드(2번째 vLLM) — standalone compose. 메인 스택과 독립.
#
# Docker Compose 는 단일 호스트만 제어하므로, 서브 노드는 Docker Context(ssh://)로
# 원격 제어한다 — 두 머신을 Docker 네이티브로 묶는 표준 방식. compose 파일/env 는
# 메인에서 읽고(클라이언트), 컨테이너 실행만 서브 데몬에서 일어난다. 서브 노드에는
# 어떤 파일도 두지 않는다(파일 전송 불필요).
#
# ⚠ bind-mount(모델 볼륨)는 *원격 데몬* 기준으로 해석되므로, compose 의 host 경로는
#   서브 노드의 절대경로여야 한다 → onprem.sub.env 의 VLLM_MODELS_HOST_DIR 로 주입.
#
# 접속 정보는 변수로 오버라이드 가능: `make up-onprem SUB_SSH=rx@10.0.0.9`
SUB_SSH ?= rx@192.168.100.11
SUB_CTX ?= onprem-sub
COMPOSE_ONPREM_SUB := docker -c $(SUB_CTX) compose \
  -f infra/compose/compose.onprem.sub.yml --env-file infra/env/onprem.sub.env

.PHONY: help build up-local down logs ps test test-integration smoke smoke-stream seed seed-encode opensearch-init os-snapshot os-restore os-snapshots verify-w1 fmt clean migrate psql prompts-validate \
  build-onprem up-onprem up-onprem-main up-onprem-sub down-onprem down-onprem-main down-onprem-sub \
  logs-onprem logs-onprem-sub ps-onprem ps-onprem-sub clean-onprem export-onprem _onprem-sub-ctx _guard-local-only \
  build-onprem-bedrock up-onprem-bedrock down-onprem-bedrock logs-onprem-bedrock ps-onprem-bedrock \
  aws-ecr-login aws-build aws-push aws-deploy aws-setup aws-setup-s3 aws-destroy aws-ssh aws-logs aws-status aws-secrets-put

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
	@echo "On-premise (air-gapped + local vLLM, 2 nodes):"
	@echo "  sub node 는 Docker Context(ssh://)로 원격 제어 — 파일은 메인에만 존재."
	@echo "  build-onprem     Build agent-api/open-webui images for onprem profile (main node)"
	@echo "  up-onprem        Bring up BOTH nodes (main stack local + sub vLLM via docker context)"
	@echo "  up-onprem-main   Bring up main node stack only"
	@echo "  up-onprem-sub    Bring up sub node vLLM only (docker -c \$$(SUB_CTX) → \$$(SUB_SSH))"
	@echo "  down-onprem      Tear down BOTH nodes (keeps volumes)"
	@echo "  down-onprem-main Tear down main node stack only"
	@echo "  down-onprem-sub  Tear down sub node vLLM only (remote)"
	@echo "  logs-onprem      Tail agent-api logs (main node)"
	@echo "  logs-onprem-sub  Tail sub node vLLM logs (remote)"
	@echo "  ps-onprem        Show main node stack status"
	@echo "  ps-onprem-sub    Show sub node vLLM status (remote)"
	@echo "  up-onprem-bedrock   Bring up main stack with second=AWS Bedrock Haiku (no sub vLLM)"
	@echo "  down-onprem-bedrock Tear down the Bedrock-sub onprem stack"
	@echo "  logs-onprem-bedrock Tail agent-api logs (Bedrock-sub variant)"
	@echo "  ps-onprem-bedrock   Show Bedrock-sub onprem stack status"
	@echo "  clean-onprem     Tear down main node stack and remove volumes"
	@echo "  export-onprem    Collect run data (events/traces/memory) → analysis dataset"

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

# OpenSearch fs-snapshot — 프로젝트 경로(infra/opensearch/snapshots/)에 저장/복원.
# local 프로파일 전용. 인덱스 기본값은 nrc-* glob (OS_SNAPSHOT_INDICES 로 override).
# ⚠ 마운트는 컨테이너 재생성 시 활성화된다. 이미 떠 있는 opensearch 라면 먼저
#   `make up-local` (또는 `... up -d opensearch`) 로 재생성해야 스냅샷이 프로젝트
#   경로에 실제로 기록된다. (path.repo 화이트리스트는 마운트와 무관하므로, 재생성
#   없이 실행하면 컨테이너 임시 레이어에 써졌다가 다음 up 에서 사라진다.)
#   make os-snapshot NAME=snap-2026-06-04
#   make os-restore  NAME=snap-2026-06-04
#   make os-snapshots
os-snapshot: _guard-local-only
	@test -n "$(NAME)" || { echo >&2 "ERROR: NAME=<snapshot-name> 필요"; exit 2; }
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	sh scripts/opensearch_snapshot.sh create $(NAME)

os-restore: _guard-local-only
	@test -n "$(NAME)" || { echo >&2 "ERROR: NAME=<snapshot-name> 필요"; exit 2; }
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	sh scripts/opensearch_snapshot.sh restore $(NAME)

os-snapshots: _guard-local-only
	OPENSEARCH_ENDPOINT=http://localhost:9200 \
	sh scripts/opensearch_snapshot.sh list

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

# ── On-premise targets (2 nodes) ──────────────────────────────────────────
# 메인 노드에서 `make <target>` 으로 양 노드(메인 전체 스택 + 서브 vLLM)를 제어한다.
# Docker Compose 는 단일 호스트만 제어하므로, 서브 노드는 Docker Context(ssh://)로
# 원격 데몬을 제어한다 — compose 파일/env 는 메인에만 두고 실행만 서브에서 일어난다.
#
# 사전 준비:
#   [메인] 1) compressed-tensors(gemma4) 지원 vLLM 이미지를 호스트 docker 데몬에 적재
#          2) ./models/gemma4-awq 에 gemma-4-26B-A4B-it-AWQ-4bit 양자화 가중치 사전 적재
#          3) (선택) hf_cache 볼륨에 임베더(e5/fermi) 사전 동기화 (HF_HUB_OFFLINE=1)
#   [서브] 1) 서브 노드 docker 데몬에 vLLM 이미지 + NVIDIA Container Toolkit
#          2) 메인→서브 SSH 키 기반 무인 접속(BatchMode) 가능 (docker context 가 재사용)
#          3) 모델 가중치를 서브 노드 절대경로에 적재하고 onprem.sub.env 의
#             VLLM_MODELS_HOST_DIR 로 그 경로를 지정(bind-mount 는 원격 데몬 기준 해석).
build-onprem:
	$(COMPOSE_ONPREM) build agent-api open-webui

# 서브 노드 제어용 docker context 를 보장한다(없으면 생성, 엔드포인트 달라지면 갱신).
# ssh:// 엔드포인트는 메인의 SSH 설정/키를 그대로 재사용한다(BatchMode 무인 접속 전제).
_onprem-sub-ctx:
	@docker context inspect $(SUB_CTX) >/dev/null 2>&1 \
	  && docker context update $(SUB_CTX) --docker "host=ssh://$(SUB_SSH)" >/dev/null \
	  || docker context create $(SUB_CTX) --docker "host=ssh://$(SUB_SSH)" >/dev/null
	@echo "==> [서브] docker context '$(SUB_CTX)' → ssh://$(SUB_SSH)"

# 양 노드 동시 기동 (메인 로컬 + 서브 원격 데몬). 서브 vLLM 은 LLM_POOL `gemma-4-26b-sub` 가 가리킨다.
up-onprem: up-onprem-main up-onprem-sub
	@echo ""
	@echo "양 노드 기동 완료 (vLLM healthy 확인됨). 상태: make ps-onprem / make ps-onprem-sub"

up-onprem-main:
	@echo "==> [메인] onprem 스택 기동"
	$(COMPOSE_ONPREM) up -d

up-onprem-sub: _onprem-sub-ctx
	@echo "==> [서브] vLLM 기동 (remote daemon)"
	$(COMPOSE_ONPREM_SUB) up -d --wait

# 양 노드 동시 종료.
down-onprem: down-onprem-main down-onprem-sub

down-onprem-main:
	$(COMPOSE_ONPREM) down

down-onprem-sub: _onprem-sub-ctx
	$(COMPOSE_ONPREM_SUB) down

# onprem(Bedrock-sub) — 단일 호스트. second(relevance/multihop)는 AWS Bedrock Haiku.
# 서브 vLLM 원격 기동 불필요 → up-onprem-sub 없이 메인 스택만 띄운다.
build-onprem-bedrock:
	$(COMPOSE_ONPREM_BEDROCK) build agent-api open-webui

up-onprem-bedrock:
	@echo "==> [메인] onprem 스택 기동 (second=AWS Bedrock Haiku)"
	$(COMPOSE_ONPREM_BEDROCK) up -d

down-onprem-bedrock:
	$(COMPOSE_ONPREM_BEDROCK) down

logs-onprem-bedrock:
	$(COMPOSE_ONPREM_BEDROCK) logs -f agent-api

ps-onprem-bedrock:
	$(COMPOSE_ONPREM_BEDROCK) ps

logs-onprem:
	$(COMPOSE_ONPREM) logs -f agent-api

logs-onprem-sub: _onprem-sub-ctx
	$(COMPOSE_ONPREM_SUB) logs -f

ps-onprem:
	$(COMPOSE_ONPREM) ps

ps-onprem-sub: _onprem-sub-ctx
	$(COMPOSE_ONPREM_SUB) ps

clean-onprem:
	$(COMPOSE_ONPREM) down -v

# Agent 실행 데이터(질의 입출력·검색 기록·트레이스·메모리)를 호스트로 한 번에
# 내려받아 interaction_id 기준 분석용 단일 데이터셋으로 평탄화한다. 폐쇄망 전제 —
# 떠 있는 onprem 컨테이너의 내부 포트/볼륨에서만 읽고 외부 전송 없음. read-only 라
# _guard-local-only 와 무관(시드/--recreate 경로를 거치지 않음).
#   make export-onprem                 # 전체 → export/<UTC stamp>/
#   make export-onprem NEWER_THAN=7d   # 최근 7일 MinIO 객체만
export-onprem:
	NEWER_THAN="$(NEWER_THAN)" scripts/export_collect.sh

# ── AWS MVP frontend deployment ───────────────────────────────────────────
# 사내 MVP 용 단일 EC2 + Caddy + Tailscale 토폴로지. 백엔드는 온프레미스 유지.
# 자세한 절차는 infra/aws/README.md 참조.
#
# 사전:
#   - aws cli v2, 적절한 IAM 권한 (rx-agent-mvp-deployer-policy)
#   - AWS_REGION 기본 ap-northeast-2 (override 시: `make aws-deploy AWS_REGION=us-west-2`)
#   - 첫 실행 전 `make aws-setup` 으로 EC2/EBS/EIP/SG/IAM Role 생성
#   - SSM Parameter Store 에 시크릿 3개 등록 (`make aws-secrets-put` 또는 README §3)

AWS_REGION    ?= ap-northeast-2
AWS_ACCOUNT   := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)
ECR_REGISTRY  := $(AWS_ACCOUNT).dkr.ecr.$(AWS_REGION).amazonaws.com
FRONTEND_REPO := agent-saas/frontend
FRONTEND_TAG  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
FRONTEND_IMAGE := $(ECR_REGISTRY)/$(FRONTEND_REPO):$(FRONTEND_TAG)

aws-ecr-login:
	@test -n "$(AWS_ACCOUNT)" || (echo "[aws] AWS_ACCOUNT 확인 실패. aws cli 자격증명을 점검하세요." && exit 1)
	aws ecr describe-repositories --region $(AWS_REGION) --repository-names $(FRONTEND_REPO) >/dev/null 2>&1 \
	  || aws ecr create-repository --region $(AWS_REGION) --repository-name $(FRONTEND_REPO) \
	       --image-scanning-configuration scanOnPush=true \
	       --image-tag-mutability MUTABLE
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ECR_REGISTRY)

aws-build:
	docker build -t $(FRONTEND_IMAGE) -t $(ECR_REGISTRY)/$(FRONTEND_REPO):latest ./frontend
	@echo ">>> Built: $(FRONTEND_IMAGE)"

aws-push: aws-ecr-login aws-build
	docker push $(FRONTEND_IMAGE)
	docker push $(ECR_REGISTRY)/$(FRONTEND_REPO):latest
	@echo ">>> Pushed: $(FRONTEND_IMAGE)"

aws-setup:
	AWS_REGION=$(AWS_REGION) ECR_REGISTRY=$(ECR_REGISTRY) ./infra/aws/setup-ec2.sh

# 일회성 — config 운반용 S3 버킷 + EC2 role read 권한 프로비저닝.
# 이미 배포된(=aws-setup 재실행 불가) 인스턴스에 S3 운반을 적용할 때 1회 실행.
aws-setup-s3:
	AWS_REGION=$(AWS_REGION) ./infra/aws/setup-s3-config.sh

aws-secrets-put:
	AWS_REGION=$(AWS_REGION) ./infra/aws/secrets-put.sh

aws-deploy: aws-push
	AWS_REGION=$(AWS_REGION) FRONTEND_IMAGE=$(FRONTEND_IMAGE) ./infra/aws/deploy.sh

aws-ssh:
	@test -f infra/aws/.state/instance-id || (echo "[aws] infra/aws/.state/instance-id 없음. 'make aws-setup' 먼저 실행." && exit 1)
	aws ssm start-session --region $(AWS_REGION) --target $$(cat infra/aws/.state/instance-id)

aws-logs:
	@test -f infra/aws/.state/instance-id || (echo "[aws] infra/aws/.state/instance-id 없음." && exit 1)
	aws ssm send-command --region $(AWS_REGION) \
	  --instance-ids $$(cat infra/aws/.state/instance-id) \
	  --document-name AWS-RunShellScript \
	  --parameters 'commands=["cd /opt/agent-saas && docker compose --env-file infra/env/aws-mvp.env --env-file /etc/agent-frontend/aws-mvp.secret.env -f infra/compose/compose.aws-mvp.yml logs --tail=200 --no-color"]' \
	  --query 'Command.CommandId' --output text \
	  | xargs -I{} sh -c 'sleep 4 && aws ssm get-command-invocation --region $(AWS_REGION) --instance-id $$(cat infra/aws/.state/instance-id) --command-id {} --query StandardOutputContent --output text'

aws-status:
	@test -f infra/aws/.state/instance-id || (echo "[aws] infra/aws/.state/instance-id 없음." && exit 1)
	aws ec2 describe-instances --region $(AWS_REGION) \
	  --instance-ids $$(cat infra/aws/.state/instance-id) \
	  --query 'Reservations[].Instances[].{ID:InstanceId,State:State.Name,EIP:PublicIpAddress,Type:InstanceType}' \
	  --output table

aws-destroy:
	AWS_REGION=$(AWS_REGION) ./infra/aws/destroy.sh
