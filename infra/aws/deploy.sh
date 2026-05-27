#!/usr/bin/env bash
# AWS frontend 재배포 — SSM Send-Command 로 EC2 위 컨테이너만 교체.
#
# 환경변수 (Makefile 에서 주입):
#   AWS_REGION      (필수)
#   FRONTEND_IMAGE  (필수, e.g. 123.dkr.ecr.ap-northeast-2.amazonaws.com/agent-saas/frontend:abc123)
#
# 동작:
#   1. .state/instance-id 의 EC2 에 SSM 명령 송신
#   2. ECR 로그인 → `docker pull` → `docker compose up -d` (open-webui 만 교체)
#   3. 명령 완료 대기 + 결과 출력

set -euo pipefail

REGION="${AWS_REGION:?AWS_REGION required}"
IMAGE="${FRONTEND_IMAGE:?FRONTEND_IMAGE required}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
INSTANCE_ID_FILE="${STATE_DIR}/instance-id"

[ -f "${INSTANCE_ID_FILE}" ] || { echo "[deploy] ${INSTANCE_ID_FILE} 없음. make aws-setup 먼저." >&2; exit 1; }
INSTANCE_ID=$(cat "${INSTANCE_ID_FILE}")

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }

log "Target: ${INSTANCE_ID} (region=${REGION})"
log "Image:  ${IMAGE}"

# ECR_REGISTRY 는 이미지 경로 앞부분
ECR_REGISTRY="${IMAGE%%/*}"

# Send-Command — heredoc 으로 원격 셸 스크립트 송신
CMD_ID=$(aws ssm send-command --region "${REGION}" \
    --instance-ids "${INSTANCE_ID}" \
    --document-name AWS-RunShellScript \
    --comment "Deploy ${IMAGE}" \
    --parameters "{
      \"commands\":[
        \"set -e\",
        \"cd /opt/agent-saas\",
        \"git fetch --depth=1 origin && git reset --hard origin/main\",
        \"aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}\",
        \"export FRONTEND_IMAGE=${IMAGE}\",
        \"docker compose --env-file infra/env/aws-mvp.env --env-file /etc/agent-frontend/aws-mvp.secret.env -f infra/compose/compose.aws-mvp.yml pull\",
        \"docker compose --env-file infra/env/aws-mvp.env --env-file /etc/agent-frontend/aws-mvp.secret.env -f infra/compose/compose.aws-mvp.yml up -d --remove-orphans\",
        \"docker image prune -f\",
        \"docker compose --env-file infra/env/aws-mvp.env --env-file /etc/agent-frontend/aws-mvp.secret.env -f infra/compose/compose.aws-mvp.yml ps\"
      ],
      \"executionTimeout\":[\"300\"]
    }" \
    --query 'Command.CommandId' --output text)

log "CommandId=${CMD_ID}, 결과 대기..."

# 최대 5분 폴링
for _ in $(seq 1 60); do
    sleep 5
    STATUS=$(aws ssm get-command-invocation --region "${REGION}" \
        --command-id "${CMD_ID}" --instance-id "${INSTANCE_ID}" \
        --query 'Status' --output text 2>/dev/null || echo "Pending")
    case "${STATUS}" in
      Success)  break ;;
      Failed|Cancelled|TimedOut)
        log "Status=${STATUS}"
        aws ssm get-command-invocation --region "${REGION}" \
          --command-id "${CMD_ID}" --instance-id "${INSTANCE_ID}" \
          --query '[StandardOutputContent,StandardErrorContent]' --output text
        exit 1
        ;;
      *) printf '.' ;;
    esac
done
echo

OUT=$(aws ssm get-command-invocation --region "${REGION}" \
    --command-id "${CMD_ID}" --instance-id "${INSTANCE_ID}" \
    --query 'StandardOutputContent' --output text)
echo "${OUT}"

log "배포 완료."
