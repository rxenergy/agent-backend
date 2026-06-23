#!/bin/bash
# EC2 user-data — AWS MVP frontend bootstrap (옵션 A: git-less).
#
# 이 파일은 setup-ec2.sh 가 placeholder 들을 치환한 뒤
# .state/user-data.rendered.sh 로 저장해 EC2 launch 시 주입한다.
# 직접 실행하지 말 것.
#
# 치환되는 placeholder (주의: 아래 목록은 토큰을 의도적으로 분리 표기한다.
# setup-ec2.sh 의 sed -g 치환이 이 주석까지 확장해 user-data 를 2배로
# 부풀리는 것을 막기 위함 — 실제 토큰은 @@ 로 감싼 한 곳에서만 등장해야 한다):
#   AWS_REGION     -> AWS region
#   ECR_REGISTRY   -> ECR registry URL (account.dkr.ecr.region.amazonaws.com)
#   COMPOSE_B64    -> infra/compose/compose.aws-mvp.yml base64 (line-wrap 없음)
#   ENV_B64        -> infra/env/aws-mvp.env base64
#   CADDY_B64      -> infra/caddy/Caddyfile base64
#   LITELLM_B64    -> infra/litellm/config.yaml base64
#
# Git 의존성 없음. 3개 config 파일을 user-data 에 직접 박아 EC2 에 기록.
# 재배포 시 새 config 가 필요하면 deploy.sh 가 SSM Send-Command 로 갱신.

set -euo pipefail
exec > >(tee -a /var/log/user-data.log) 2>&1
echo "[user-data] start at $(date -Iseconds)"

REGION="@@AWS_REGION@@"
ECR_REGISTRY="@@ECR_REGISTRY@@"

# 1. Packages
dnf -y update
dnf -y install docker jq

mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

systemctl enable --now docker
usermod -aG docker ec2-user || true

# 2. AWS CLI v2 (AL2023 에 v1 이 있을 수 있으나 v2 가 안정적)
if ! command -v aws >/dev/null 2>&1 || ! aws --version 2>&1 | grep -q "aws-cli/2"; then
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
    dnf -y install unzip
    (cd /tmp && unzip -q -o awscli.zip && ./aws/install --update)
fi

# 3. Tailscale
curl -fsSL https://pkgs.tailscale.com/stable/amazon-linux/2023/tailscale.repo \
    -o /etc/yum.repos.d/tailscale.repo
dnf -y install tailscale
systemctl enable --now tailscaled

TS_AUTHKEY=$(aws ssm get-parameter --region "${REGION}" \
    --name /rx-agent/frontend/tailscale_authkey \
    --with-decryption --query Parameter.Value --output text)
tailscale up \
    --authkey="${TS_AUTHKEY}" \
    --hostname=aws-frontend \
    --advertise-tags=tag:frontend-aws \
    --ssh \
    --accept-dns=true || echo "[user-data] tailscale up 실패 — 수동 점검 필요"

# 4. EBS 데이터 볼륨 마운트 (멱등)
DATA_DEV=""
for c in /dev/nvme1n1 /dev/xvdf /dev/sdf; do
    [ -b "$c" ] && DATA_DEV="$c" && break
done
if [ -n "${DATA_DEV}" ]; then
    if ! blkid "${DATA_DEV}" >/dev/null 2>&1; then
        mkfs.xfs -f "${DATA_DEV}"
    fi
    mkdir -p /data
    if ! grep -q "/data" /etc/fstab; then
        UUID=$(blkid -s UUID -o value "${DATA_DEV}")
        echo "UUID=${UUID} /data xfs defaults,nofail 0 2" >> /etc/fstab
    fi
    mount -a
    mkdir -p /data/open-webui /data/caddy/data /data/caddy/config
    chown -R 1000:1000 /data/open-webui
else
    echo "[user-data] 경고: 데이터 볼륨 미발견. /data 가 루트 디스크 위에 만들어짐"
    mkdir -p /data/open-webui /data/caddy/data /data/caddy/config
fi

# 4.5 swap (멱등) — t3.small(2GB RAM)에 컨테이너 3개(open-webui+caddy+litellm)를
# 올리면 메모리가 빠듯해 OOM 으로 호스트가 행에 빠진다. 2GB swapfile 로 완충한다.
# swappiness 를 낮춰(20) 평시엔 RAM 우선, 압박 시에만 swap 으로 흘린다.
if ! swapon --show | grep -q '/swapfile'; then
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
fi
grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=20 || true
grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=20' >> /etc/sysctl.conf

# 5. 시크릿 → /etc/agent-frontend/aws-mvp.secret.env
mkdir -p /etc/agent-frontend
chmod 700 /etc/agent-frontend

ssm_get() {
    aws ssm get-parameter --region "${REGION}" \
        --name "$1" --with-decryption --query Parameter.Value --output text
}

WEBUI_SECRET_KEY_VAL=$(ssm_get /rx-agent/frontend/webui_secret_key)
# 온프레 agent-api 호출 토큰(connection[0]). 백엔드가 검증 안 하면 'dummy'.
ONPREM_TOKEN=$(ssm_get /rx-agent/frontend/openai_api_key)
# Bedrock bearer token → litellm. litellm master key → OpenWebUI 가 litellm 호출 시 사용.
BEDROCK_API_KEY_VAL=$(ssm_get /rx-agent/frontend/bedrock_api_key)
LITELLM_MASTER_KEY_VAL=$(ssm_get /rx-agent/frontend/litellm_master_key)

{
    echo "WEBUI_SECRET_KEY=${WEBUI_SECRET_KEY_VAL}"
    # OPENAI_API_BASE_URLS 와 index 로 짝지음: [0]=온프레 토큰, [1]=litellm master key.
    echo "OPENAI_API_KEYS=${ONPREM_TOKEN};${LITELLM_MASTER_KEY_VAL}"
    # litellm 컨테이너용.
    echo "AWS_BEARER_TOKEN_BEDROCK=${BEDROCK_API_KEY_VAL}"
    echo "LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY_VAL}"
} > /etc/agent-frontend/aws-mvp.secret.env
chmod 600 /etc/agent-frontend/aws-mvp.secret.env

# 6. Config 파일을 /opt/agent-saas/ 에 기록 (git clone 대신 base64 inline)
#    deploy.sh 가 추후 같은 위치를 SSM 으로 덮어쓴다.
APP_DIR=/opt/agent-saas
mkdir -p "${APP_DIR}/infra/compose" "${APP_DIR}/infra/env" "${APP_DIR}/infra/caddy" \
         "${APP_DIR}/infra/litellm"

echo "@@COMPOSE_B64@@" | base64 -d > "${APP_DIR}/infra/compose/compose.aws-mvp.yml"
echo "@@ENV_B64@@"     | base64 -d > "${APP_DIR}/infra/env/aws-mvp.env"
echo "@@CADDY_B64@@"   | base64 -d > "${APP_DIR}/infra/caddy/Caddyfile"
echo "@@LITELLM_B64@@" | base64 -d > "${APP_DIR}/infra/litellm/config.yaml"

echo "[user-data] config 파일 기록 완료:"
ls -la "${APP_DIR}/infra/compose/" "${APP_DIR}/infra/env/" \
       "${APP_DIR}/infra/caddy/" "${APP_DIR}/infra/litellm/"

# 7. ECR 로그인 (인스턴스 Role 의 ECR ReadOnly 권한 사용)
aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ECR_REGISTRY}"

# 8. 첫 실행 시 latest 태그로 시작. 이후 `make aws-deploy` 가 SHA 태그로 갱신.
cd "${APP_DIR}/infra/compose"
export FRONTEND_IMAGE="${ECR_REGISTRY}/agent-saas/frontend:latest"

if docker pull "${FRONTEND_IMAGE}"; then
    docker compose \
        --env-file ../env/aws-mvp.env \
        --env-file /etc/agent-frontend/aws-mvp.secret.env \
        -f compose.aws-mvp.yml up -d
else
    echo "[user-data] ${FRONTEND_IMAGE} 아직 없음 — make aws-deploy 로 첫 푸시 진행 필요"
fi

echo "[user-data] end at $(date -Iseconds)"
