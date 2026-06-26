#!/usr/bin/env bash
# AWS frontend MVP 리소스 전부 삭제. EBS 데이터 볼륨도 함께 사라지므로
# 사용자/대화 이력은 사전에 백업 필요.
#
# 환경변수:
#   AWS_REGION (필수)

set -euo pipefail

REGION="${AWS_REGION:?AWS_REGION required}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"

log() { printf '\033[1;33m[destroy]\033[0m %s\n' "$*"; }

read -rp "AWS frontend MVP 리소스를 정말 모두 삭제합니까? (y/N) " ans
[ "${ans}" = "y" ] || { echo "중단."; exit 0; }

# 1. Instance terminate
if [ -f "${STATE_DIR}/instance-id" ]; then
    IID=$(cat "${STATE_DIR}/instance-id")
    log "Terminating ${IID}"
    aws ec2 terminate-instances --region "${REGION}" --instance-ids "${IID}" >/dev/null
    aws ec2 wait instance-terminated --region "${REGION}" --instance-ids "${IID}"
fi

# 2. EIP release
if [ -f "${STATE_DIR}/eip-alloc" ]; then
    EIP_ALLOC=$(cat "${STATE_DIR}/eip-alloc")
    log "Releasing EIP ${EIP_ALLOC}"
    aws ec2 release-address --region "${REGION}" --allocation-id "${EIP_ALLOC}" || true
fi

# 3. Security Group
log "Security Group 삭제 (있으면)"
SG_ID=$(aws ec2 describe-security-groups --region "${REGION}" \
    --filters "Name=group-name,Values=rx-agent-frontend-sg" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)
[ "${SG_ID}" != "None" ] && [ -n "${SG_ID}" ] && \
    aws ec2 delete-security-group --region "${REGION}" --group-id "${SG_ID}" || true

# 4. KeyPair
log "KeyPair 삭제"
aws ec2 delete-key-pair --region "${REGION}" --key-name rx-agent-frontend-key 2>/dev/null || true

# 5. IAM Role + Instance Profile
log "IAM 정리"
aws iam remove-role-from-instance-profile \
  --instance-profile-name rx-agent-frontend-ec2 \
  --role-name rx-agent-frontend-ec2 2>/dev/null || true
aws iam delete-instance-profile \
  --instance-profile-name rx-agent-frontend-ec2 2>/dev/null || true
aws iam detach-role-policy --role-name rx-agent-frontend-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore 2>/dev/null || true
aws iam detach-role-policy --role-name rx-agent-frontend-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly 2>/dev/null || true
aws iam delete-role-policy --role-name rx-agent-frontend-ec2 \
  --policy-name ssm-parameter-read 2>/dev/null || true
# config S3 버킷 read 인라인 정책 — 남아 있으면 delete-role 이 실패한다.
aws iam delete-role-policy --role-name rx-agent-frontend-ec2 \
  --policy-name config-bucket-read 2>/dev/null || true
aws iam delete-role --role-name rx-agent-frontend-ec2 2>/dev/null || true

# 6. State files
log "state 디렉토리 삭제: ${STATE_DIR}"
rm -rf "${STATE_DIR}"

cat <<EOF

╔═══════════════════════════════════════════════════════════════════════╗
║ 삭제 완료                                                             ║
╠═══════════════════════════════════════════════════════════════════════╣
║ 수동으로 정리해야 할 것:                                              ║
║   - SSM Parameter Store: /rx-agent/frontend/* (시크릿)                ║
║     (보존하려면 그대로 둠. 삭제하려면 콘솔/CLI 로 별도 제거)          ║
║   - S3 config 버킷: rx-agent-frontend-config-<acct> (버저닝 ON)       ║
║     (config 이력 보존 위해 자동 삭제 안 함. 삭제: aws s3 rb --force)  ║
║   - ECR 레포: agent-saas/frontend (이미지 보존을 위해 자동 삭제 안 함)║
║   - 회사 도메인 등록기관의 A 레코드                                   ║
║   - Tailscale 어드민의 aws-frontend 머신 항목                         ║
╚═══════════════════════════════════════════════════════════════════════╝
EOF
