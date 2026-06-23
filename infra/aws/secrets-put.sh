#!/usr/bin/env bash
# SSM Parameter Store 에 시크릿 5개를 등록한다.
# 이미 존재하면 그대로 두고, 없을 때만 prompt 로 입력 받아 등록.
#
# 등록 항목:
#   /rx-agent/frontend/webui_secret_key      — OpenWebUI 세션 서명 키 (랜덤 자동 생성)
#   /rx-agent/frontend/openai_api_key        — 온프레 agent-api 호출 토큰 (dummy 또는 실제 키)
#   /rx-agent/frontend/tailscale_authkey     — Tailscale auth key (admin console 발급)
#   /rx-agent/frontend/bedrock_api_key       — Bedrock 임시 API 키(bearer token) → litellm
#   /rx-agent/frontend/litellm_master_key    — OpenWebUI→litellm 인증 키 (랜덤 자동 생성)

set -euo pipefail

# read -rs 중 Ctrl+C/오류로 종료될 때 터미널 echo 가 꺼진 채로 남는 것을 방지.
restore_tty() { stty echo 2>/dev/null || true; }
trap restore_tty EXIT INT TERM

REGION="${AWS_REGION:?AWS_REGION required}"

log()  { printf '\033[1;34m[secrets-put]\033[0m %s\n' "$*"; }

put_if_missing() {
    local name="$1"; local prompt="$2"; local default_gen="${3:-}"
    if aws ssm get-parameter --region "${REGION}" --name "${name}" >/dev/null 2>&1; then
        log "  ${name} : 이미 존재 (skip)"
        return
    fi
    local value=""
    if [ -n "${default_gen}" ]; then
        value=$(eval "${default_gen}")
        log "  ${name} : 자동 생성 (랜덤)"
    else
        printf '  %s 값을 입력하세요 (입력은 표시되지 않음): ' "${prompt}"
        read -rs value; echo
        [ -n "${value}" ] || { echo "[!] 빈 값. 건너뜀." >&2; return; }
    fi
    aws ssm put-parameter --region "${REGION}" \
      --name "${name}" --type SecureString --value "${value}" \
      --tags "Key=Project,Value=rx-agent-mvp" >/dev/null
    log "  ${name} : 등록 완료"
}

log "SSM Parameter Store (region=${REGION}) 시크릿 등록"

put_if_missing "/rx-agent/frontend/webui_secret_key" \
    "OpenWebUI session key" \
    "openssl rand -hex 32"

put_if_missing "/rx-agent/frontend/openai_api_key" \
    "백엔드 호출 토큰 (백엔드가 검증하지 않으면 'dummy' 입력)"

put_if_missing "/rx-agent/frontend/tailscale_authkey" \
    "Tailscale reusable auth key (https://login.tailscale.com/admin/settings/keys)"

put_if_missing "/rx-agent/frontend/bedrock_api_key" \
    "Bedrock 임시 API 키 (bedrock-api-key-... bearer token)"

put_if_missing "/rx-agent/frontend/litellm_master_key" \
    "litellm master key" \
    "openssl rand -hex 32"

log "완료. 다음 단계: make aws-setup (첫 실행) 또는 make aws-deploy (이미 setup 완료 시)"
