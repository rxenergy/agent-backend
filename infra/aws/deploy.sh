#!/usr/bin/env bash
# AWS frontend 재배포 — git 없이 SSM Send-Command 로 EC2 위 config + 컨테이너 교체.
#
# 환경변수 (Makefile 에서 주입):
#   AWS_REGION      (필수)
#   FRONTEND_IMAGE  (필수, e.g. 123.dkr.ecr.ap-northeast-2.amazonaws.com/agent-saas/frontend:abc123)
#
# 동작:
#   1. 로컬의 3개 config 파일을 base64 로 인코딩
#   2. SSM Send-Command 1발로 EC2 에 전송:
#        - base64 decode 해서 /opt/agent-saas/ 에 덮어쓰기
#        - ECR 로그인 → docker pull → docker compose up -d
#   3. 명령 완료 대기 + 결과 출력

set -euo pipefail

REGION="${AWS_REGION:?AWS_REGION required}"
IMAGE="${FRONTEND_IMAGE:?FRONTEND_IMAGE required}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
INSTANCE_ID_FILE="${STATE_DIR}/instance-id"

[ -f "${INSTANCE_ID_FILE}" ] || { echo "[deploy] ${INSTANCE_ID_FILE} 없음. make aws-setup 먼저." >&2; exit 1; }
INSTANCE_ID=$(cat "${INSTANCE_ID_FILE}")

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }

log "Target: ${INSTANCE_ID} (region=${REGION})"
log "Image:  ${IMAGE}"

ECR_REGISTRY="${IMAGE%%/*}"

# 로컬 config 파일을 base64 로 (단일행, line-wrap 없이)
COMPOSE_B64=$(base64 -w0 < "${REPO_ROOT}/infra/compose/compose.aws-mvp.yml")
ENV_B64=$(base64 -w0     < "${REPO_ROOT}/infra/env/aws-mvp.env")
CADDY_B64=$(base64 -w0   < "${REPO_ROOT}/infra/caddy/Caddyfile")
LITELLM_B64=$(base64 -w0 < "${REPO_ROOT}/infra/litellm/config.yaml")

# SSM Send-Command 의 parameter list 는 JSON. base64 는 안전한 alphabet 이라
# 그대로 commands 배열에 박을 수 있다. payload 합산 100KB 미만이면 OK.
TOTAL=$((${#COMPOSE_B64} + ${#ENV_B64} + ${#CADDY_B64} + ${#LITELLM_B64}))
log "Config payload: ${TOTAL} bytes (limit ~100KB)"

# 임시 JSON parameters 파일 (긴 문자열을 안전하게 전달)
PARAMS_FILE=$(mktemp)
trap 'rm -f "${PARAMS_FILE}"' EXIT

cat > "${PARAMS_FILE}" <<EOF
{
  "commands": [
    "set -e",
    "mkdir -p /opt/agent-saas/infra/compose /opt/agent-saas/infra/env /opt/agent-saas/infra/caddy /opt/agent-saas/infra/litellm",
    "echo ${COMPOSE_B64} | base64 -d > /opt/agent-saas/infra/compose/compose.aws-mvp.yml",
    "echo ${ENV_B64} | base64 -d > /opt/agent-saas/infra/env/aws-mvp.env",
    "echo ${CADDY_B64} | base64 -d > /opt/agent-saas/infra/caddy/Caddyfile",
    "echo ${LITELLM_B64} | base64 -d > /opt/agent-saas/infra/litellm/config.yaml",
    "mkdir -p /etc/agent-frontend && chmod 700 /etc/agent-frontend",
    "WK=\$(aws ssm get-parameter --region ${REGION} --name /rx-agent/frontend/webui_secret_key --with-decryption --query Parameter.Value --output text)",
    "ON=\$(aws ssm get-parameter --region ${REGION} --name /rx-agent/frontend/openai_api_key --with-decryption --query Parameter.Value --output text)",
    "BK=\$(aws ssm get-parameter --region ${REGION} --name /rx-agent/frontend/bedrock_api_key --with-decryption --query Parameter.Value --output text)",
    "LK=\$(aws ssm get-parameter --region ${REGION} --name /rx-agent/frontend/litellm_master_key --with-decryption --query Parameter.Value --output text)",
    "printf 'WEBUI_SECRET_KEY=%s\\\\nOPENAI_API_KEYS=%s;%s\\\\nAWS_BEARER_TOKEN_BEDROCK=%s\\\\nLITELLM_MASTER_KEY=%s\\\\n' \$WK \$ON \$LK \$BK \$LK > /etc/agent-frontend/aws-mvp.secret.env",
    "chmod 600 /etc/agent-frontend/aws-mvp.secret.env",
    "mkdir -p /data/open-webui /data/caddy/data /data/caddy/config",
    "chown -R 1000:1000 /data/open-webui",
    "cd /opt/agent-saas/infra/compose",
    "aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}",
    "export FRONTEND_IMAGE=${IMAGE}",
    "docker compose --env-file ../env/aws-mvp.env --env-file /etc/agent-frontend/aws-mvp.secret.env -f compose.aws-mvp.yml pull",
    "docker compose --env-file ../env/aws-mvp.env --env-file /etc/agent-frontend/aws-mvp.secret.env -f compose.aws-mvp.yml up -d --remove-orphans",
    "docker image prune -f",
    "docker compose --env-file ../env/aws-mvp.env --env-file /etc/agent-frontend/aws-mvp.secret.env -f compose.aws-mvp.yml ps"
  ],
  "executionTimeout": ["300"]
}
EOF

CMD_ID=$(aws ssm send-command --region "${REGION}" \
    --instance-ids "${INSTANCE_ID}" \
    --document-name AWS-RunShellScript \
    --comment "Deploy ${IMAGE}" \
    --parameters "file://${PARAMS_FILE}" \
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
