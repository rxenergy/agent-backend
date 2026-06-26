#!/usr/bin/env bash
# AWS frontend 재배포 — git 없이 SSM Send-Command 로 EC2 위 config + 컨테이너 교체.
#
# 환경변수 (Makefile 에서 주입):
#   AWS_REGION      (필수)
#   FRONTEND_IMAGE  (필수, e.g. 123.dkr.ecr.ap-northeast-2.amazonaws.com/agent-saas/frontend:abc123)
#
# 동작:
#   1. 로컬 config 파일을 S3(s3://CONFIG_BUCKET/aws-mvp/)로 업로드
#   2. SSM Send-Command 1발로 EC2 에 전송:
#        - aws s3 sync 로 /opt/agent-saas/infra/ 재현
#        - 시크릿은 SSM Parameter Store 에서 secret.env 합성
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

# config 운반 S3 버킷 (setup-s3-config.sh 와 동일 규칙). 시크릿은 S3 아닌 SSM.
# 버킷·role 정책은 일회성 셋업(make aws-setup-s3 / setup-ec2.sh)이 만든다 —
# deploy 는 업로드+배포만 담당한다(IAM 변경 권한 불요).
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CONFIG_BUCKET="${CONFIG_BUCKET:-rx-agent-frontend-config-${ACCOUNT_ID}}"
CONFIG_PREFIX="aws-mvp"

# 로컬 config 를 S3 로 업로드. EC2 는 SSM 커맨드에서 `aws s3 sync` 로 받는다.
# base64+SSM inline 대신 S3 — payload 크기 한도 없음, 향후 config 증가에 안전.
# 버킷이 없으면(=셋업 미실행) 여기서 실패한다 → make aws-setup-s3 먼저 안내.
if ! aws s3api head-bucket --bucket "${CONFIG_BUCKET}" >/dev/null 2>&1; then
    echo "[deploy] config 버킷 ${CONFIG_BUCKET} 없음. 먼저 'make aws-setup-s3' 실행." >&2
    exit 1
fi
log "config → s3://${CONFIG_BUCKET}/${CONFIG_PREFIX}/"
up() { aws s3 cp "${REPO_ROOT}/infra/$1" "s3://${CONFIG_BUCKET}/${CONFIG_PREFIX}/$2" >/dev/null; }
up compose/compose.aws-mvp.yml compose/compose.aws-mvp.yml
up env/aws-mvp.env             env/aws-mvp.env
up caddy/Caddyfile             caddy/Caddyfile
up litellm/config.yaml         litellm/config.yaml
up litellm/strip_history.py    litellm/strip_history.py
up searxng/settings.yml        searxng/settings.yml

# 임시 JSON parameters 파일 (긴 문자열을 안전하게 전달)
PARAMS_FILE=$(mktemp)
trap 'rm -f "${PARAMS_FILE}"' EXIT

cat > "${PARAMS_FILE}" <<EOF
{
  "commands": [
    "set -e",
    "mkdir -p /opt/agent-saas/infra",
    "aws s3 sync s3://${CONFIG_BUCKET}/${CONFIG_PREFIX}/ /opt/agent-saas/infra/ --delete",
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
