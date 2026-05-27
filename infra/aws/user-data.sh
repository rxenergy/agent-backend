#!/bin/bash
# EC2 user-data — AWS MVP frontend bootstrap.
#
# 이 파일은 setup-ec2.sh 가 @@PLACEHOLDER@@ 들을 치환한 뒤
# .state/user-data.rendered.sh 로 저장해 EC2 launch 시 주입한다.
# 직접 실행하지 말 것.
#
# 전제:
#   - Amazon Linux 2023 (dnf), x86_64
#   - 인스턴스 IAM Role: AmazonSSMManagedInstanceCore + ECR Read + SSM Param Read
#   - 데이터 EBS 볼륨이 /dev/sdf 로 attach (AL2023 에선 /dev/nvme1n1 로 보임)
#   - SSM Parameter Store 시크릿 3개 사전 등록 (`make aws-secrets-put`)
#   - 회사 도메인 A 레코드는 EIP 부착 후 별도 등록

set -euo pipefail
exec > >(tee -a /var/log/user-data.log) 2>&1
echo "[user-data] start at $(date -Iseconds)"

REGION="@@AWS_REGION@@"
ECR_REGISTRY="@@ECR_REGISTRY@@"
REPO_URL="${REPO_URL:-https://github.com/rx-dylee/mvp-saas-agent.git}"
REPO_REF="${REPO_REF:-main}"

# 1. Packages
dnf -y update
dnf -y install docker git jq

# Docker Compose v2 plugin (dnf 의 docker-compose-plugin 패키지가 없을 수 있음)
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

systemctl enable --now docker
usermod -aG docker ec2-user || true

# 2. AWS CLI v2 (AL2023 에는 awscli-2 가 기본 포함되지 않을 수 있음)
if ! command -v aws >/dev/null 2>&1; then
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
    (cd /tmp && unzip -q awscli.zip && ./aws/install)
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
# AL2023 NVMe: 디바이스 이름이 /dev/nvme1n1 로 보인다 (lsblk 로 확인 가능)
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
    chown -R 1000:1000 /data/open-webui   # OpenWebUI 의 기본 UID
else
    echo "[user-data] 경고: 데이터 볼륨 미발견. /data 가 루트 디스크 위에 만들어짐"
    mkdir -p /data/open-webui /data/caddy/data /data/caddy/config
fi

# 5. 시크릿 → /etc/agent-frontend/aws-mvp.secret.env
mkdir -p /etc/agent-frontend
chmod 700 /etc/agent-frontend
{
    echo "WEBUI_SECRET_KEY=$(aws ssm get-parameter --region ${REGION} \
        --name /rx-agent/frontend/webui_secret_key \
        --with-decryption --query Parameter.Value --output text)"
    echo "OPENAI_API_KEY=$(aws ssm get-parameter --region ${REGION} \
        --name /rx-agent/frontend/openai_api_key \
        --with-decryption --query Parameter.Value --output text)"
} > /etc/agent-frontend/aws-mvp.secret.env
chmod 600 /etc/agent-frontend/aws-mvp.secret.env

# 6. 레포 체크아웃 (compose / env / Caddyfile 만 필요)
REPO_DIR=/opt/agent-saas
if [ ! -d "${REPO_DIR}/.git" ]; then
    git clone --depth=1 --branch "${REPO_REF}" "${REPO_URL}" "${REPO_DIR}"
fi

# 7. ECR 로그인 (인스턴스 Role 의 ECR ReadOnly 권한 사용)
aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ECR_REGISTRY}"

# 8. 첫 실행 시 latest 태그로 시작. 이후 `make aws-deploy` 가 SHA 태그로 갱신.
cd "${REPO_DIR}/infra/compose"
export FRONTEND_IMAGE="${ECR_REGISTRY}/agent-saas/frontend:latest"

# 이미지가 아직 push 되기 전일 수 있음 — 실패해도 user-data 자체는 성공으로 두고
# 사용자가 `make aws-deploy` 로 시작하도록 둔다.
if docker pull "${FRONTEND_IMAGE}"; then
    docker compose \
        --env-file ../env/aws-mvp.env \
        --env-file /etc/agent-frontend/aws-mvp.secret.env \
        -f compose.aws-mvp.yml up -d
else
    echo "[user-data] ${FRONTEND_IMAGE} 아직 없음 — make aws-deploy 로 첫 푸시 진행 필요"
fi

echo "[user-data] end at $(date -Iseconds)"
