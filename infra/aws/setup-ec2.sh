#!/usr/bin/env bash
# 1회성 — AWS MVP frontend 의 EC2/EBS/EIP/SG/IAM Role 을 생성한다.
#
# 멱등하지 않다. 처음 실행 후 생성된 ID 들은 infra/aws/.state/ 에 저장되어
# 후속 명령(make aws-deploy, make aws-ssh ...) 이 참조한다.
# 두 번째로 실행하려면 먼저 `make aws-destroy` 또는 state 파일 수동 삭제.
#
# 환경변수 (Makefile 에서 주입):
#   AWS_REGION    (필수)
#   ECR_REGISTRY  (필수, e.g. 123.dkr.ecr.ap-northeast-2.amazonaws.com)
#
# 선택 (override 가능):
#   INSTANCE_TYPE       (기본 t3.small)
#   DATA_VOLUME_SIZE_GB (기본 20)
#   PROJECT_TAG         (기본 rx-agent-mvp)
#   ALLOWED_SSH_CIDR    (기본 0.0.0.0/0 — SSM 권장이라 SSH 는 막아도 됨)
#
# 사전 등록 필요 (스크립트 실행 전):
#   1) SSM Parameter Store 시크릿 5개 — `make aws-secrets-put` 또는 README §3
#   2) 회사 도메인 등록기관에 A 레코드 추가 (출력된 EIP 사용) — 본 스크립트 이후

set -euo pipefail

REGION="${AWS_REGION:?AWS_REGION required}"
ECR_REG="${ECR_REGISTRY:?ECR_REGISTRY required}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.small}"
DATA_VOLUME_SIZE_GB="${DATA_VOLUME_SIZE_GB:-20}"
PROJECT_TAG="${PROJECT_TAG:-rx-agent-mvp}"
ALLOWED_SSH_CIDR="${ALLOWED_SSH_CIDR:-0.0.0.0/0}"

ROLE_NAME="rx-agent-frontend-ec2"
PROFILE_NAME="rx-agent-frontend-ec2"
SG_NAME="rx-agent-frontend-sg"
KEY_NAME="rx-agent-frontend-key"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
mkdir -p "${STATE_DIR}"

log()  { printf '\033[1;34m[setup-ec2]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[setup-ec2]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "${STATE_DIR}/instance-id" ] && fail "이미 setup 완료 상태입니다 (${STATE_DIR}/instance-id 존재). 먼저 make aws-destroy 후 재시도."

aws sts get-caller-identity >/dev/null || fail "aws cli 자격증명 실패. aws configure 또는 AWS_PROFILE 점검."

# config 운반용 S3 버킷. 시크릿이 아닌 config(compose/env/Caddyfile/litellm/searxng)
# 를 담는다. user-data base64 inline 의 16KB 한도를 없애기 위한 표준 패턴.
# 시크릿은 여전히 SSM Parameter Store(아래 role 정책) — S3 에는 올리지 않는다.
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CONFIG_BUCKET="${CONFIG_BUCKET:-rx-agent-frontend-config-${ACCOUNT_ID}}"
CONFIG_PREFIX="aws-mvp"

# ─────────────────────────────────────────────────────────────────────────
# 1. IAM Role + Instance Profile
# ─────────────────────────────────────────────────────────────────────────
log "1/8 IAM Role 생성: ${ROLE_NAME}"
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
    aws iam create-role --role-name "${ROLE_NAME}" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{
          "Effect":"Allow",
          "Principal":{"Service":"ec2.amazonaws.com"},
          "Action":"sts:AssumeRole"
        }]
      }' \
      --tags Key=Project,Value="${PROJECT_TAG}" >/dev/null

    aws iam attach-role-policy --role-name "${ROLE_NAME}" \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    aws iam attach-role-policy --role-name "${ROLE_NAME}" \
      --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly

    aws iam put-role-policy --role-name "${ROLE_NAME}" \
      --policy-name ssm-parameter-read \
      --policy-document "{
        \"Version\":\"2012-10-17\",
        \"Statement\":[
          {
            \"Effect\":\"Allow\",
            \"Action\":[\"ssm:GetParameter\",\"ssm:GetParameters\"],
            \"Resource\":\"arn:aws:ssm:*:*:parameter/rx-agent/frontend/*\"
          },
          {
            \"Effect\":\"Allow\",
            \"Action\":[\"kms:Decrypt\"],
            \"Resource\":\"*\",
            \"Condition\":{\"StringEquals\":{\"kms:ViaService\":\"ssm.${REGION}.amazonaws.com\"}}
          }
        ]
      }"
fi
# config S3 버킷 생성 + role 의 config-bucket-read 권한은 7단계에서
# setup-s3-config.sh 호출로 일괄 처리(중복 제거, role 생성 직후라 안전).

if ! aws iam get-instance-profile --instance-profile-name "${PROFILE_NAME}" >/dev/null 2>&1; then
    aws iam create-instance-profile --instance-profile-name "${PROFILE_NAME}" >/dev/null
    aws iam add-role-to-instance-profile \
      --instance-profile-name "${PROFILE_NAME}" \
      --role-name "${ROLE_NAME}"
    log "    Instance Profile 전파 대기 (10s)..."
    sleep 10
fi

# ─────────────────────────────────────────────────────────────────────────
# 2. VPC / Subnet (기본 VPC 사용)
# ─────────────────────────────────────────────────────────────────────────
log "2/8 기본 VPC 조회"
VPC_ID=$(aws ec2 describe-vpcs --region "${REGION}" \
    --filters "Name=is-default,Values=true" \
    --query 'Vpcs[0].VpcId' --output text)
[ "${VPC_ID}" != "None" ] || fail "default VPC 없음. 수동으로 VPC_ID 지정 필요."

SUBNET_ID=$(aws ec2 describe-subnets --region "${REGION}" \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=default-for-az,Values=true" \
    --query 'Subnets[0].SubnetId' --output text)
log "    VPC=${VPC_ID} Subnet=${SUBNET_ID}"

# ─────────────────────────────────────────────────────────────────────────
# 3. Security Group
# ─────────────────────────────────────────────────────────────────────────
log "3/8 Security Group: ${SG_NAME}"
SG_ID=$(aws ec2 describe-security-groups --region "${REGION}" \
    --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

if [ "${SG_ID}" = "None" ] || [ -z "${SG_ID}" ]; then
    SG_ID=$(aws ec2 create-security-group --region "${REGION}" \
        --group-name "${SG_NAME}" \
        --description "RX Agent MVP frontend (Caddy + OpenWebUI)" \
        --vpc-id "${VPC_ID}" \
        --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=${PROJECT_TAG}}]" \
        --query 'GroupId' --output text)

    aws ec2 authorize-security-group-ingress --region "${REGION}" \
      --group-id "${SG_ID}" --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
    aws ec2 authorize-security-group-ingress --region "${REGION}" \
      --group-id "${SG_ID}" --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null
    aws ec2 authorize-security-group-ingress --region "${REGION}" \
      --group-id "${SG_ID}" --protocol udp --port 443 --cidr 0.0.0.0/0 >/dev/null
    # 22 는 fallback. 평시엔 SSM Session Manager 권장.
    aws ec2 authorize-security-group-ingress --region "${REGION}" \
      --group-id "${SG_ID}" --protocol tcp --port 22 --cidr "${ALLOWED_SSH_CIDR}" >/dev/null
fi
log "    SG=${SG_ID}"

# ─────────────────────────────────────────────────────────────────────────
# 4. KeyPair (SSM 사용 시 선택 — 그래도 break-glass 용으로 생성)
# ─────────────────────────────────────────────────────────────────────────
log "4/8 KeyPair: ${KEY_NAME}"
if ! aws ec2 describe-key-pairs --region "${REGION}" --key-names "${KEY_NAME}" >/dev/null 2>&1; then
    aws ec2 create-key-pair --region "${REGION}" --key-name "${KEY_NAME}" \
      --query 'KeyMaterial' --output text > "${STATE_DIR}/${KEY_NAME}.pem"
    chmod 600 "${STATE_DIR}/${KEY_NAME}.pem"
    log "    PEM 저장: ${STATE_DIR}/${KEY_NAME}.pem (Git 에 절대 커밋 금지)"
fi

# ─────────────────────────────────────────────────────────────────────────
# 5. EIP
# ─────────────────────────────────────────────────────────────────────────
log "5/8 EIP 할당"
# 멱등: 이전 run 이 step 8 직전(EIP 할당 후 run-instances 실패)에서 죽었을 때
# .state/eip-alloc 에 남은 할당을 재사용한다. 그러지 않으면 재실행마다
# 미연결 EIP 가 새로 새서 과금된다 (associate 안 된 EIP 는 시간당 과금).
EIP_ALLOC=""
if [ -f "${STATE_DIR}/eip-alloc" ]; then
    PREV_ALLOC="$(cat "${STATE_DIR}/eip-alloc")"
    if aws ec2 describe-addresses --region "${REGION}" \
         --allocation-ids "${PREV_ALLOC}" >/dev/null 2>&1; then
        EIP_ALLOC="${PREV_ALLOC}"
        log "    기존 EIP 재사용: ${EIP_ALLOC}"
    fi
fi
if [ -z "${EIP_ALLOC}" ]; then
    EIP_ALLOC=$(aws ec2 allocate-address --region "${REGION}" --domain vpc \
        --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Project,Value=${PROJECT_TAG}}]" \
        --query 'AllocationId' --output text)
fi
EIP=$(aws ec2 describe-addresses --region "${REGION}" \
    --allocation-ids "${EIP_ALLOC}" --query 'Addresses[0].PublicIp' --output text)
echo "${EIP_ALLOC}" > "${STATE_DIR}/eip-alloc"
echo "${EIP}" > "${STATE_DIR}/eip"
log "    EIP=${EIP}"

# ─────────────────────────────────────────────────────────────────────────
# 6. AMI (Amazon Linux 2023 최신 x86_64)
# ─────────────────────────────────────────────────────────────────────────
log "6/8 AL2023 AMI 조회"
AMI_ID=$(aws ec2 describe-images --region "${REGION}" --owners amazon \
    --filters "Name=name,Values=al2023-ami-2023.*-kernel-*-x86_64" \
              "Name=state,Values=available" \
    --query 'Images|sort_by(@,&CreationDate)[-1].ImageId' --output text)
log "    AMI=${AMI_ID}"

# ─────────────────────────────────────────────────────────────────────────
# 7. config → S3 업로드 + user-data 렌더링
#    Git 의존성 없이 config(compose/env/Caddyfile/litellm/searxng)를 EC2 로 운반.
#    base64 inline(16KB user-data 한도) 대신 S3 를 쓴다 — 한도 없음, 향후 config
#    증가에도 안전. EC2 는 부팅 시 `aws s3 sync` 한 번으로 /opt/agent-saas/infra/
#    전체를 재현한다(인스턴스 role 의 config-bucket-read 권한 사용).
#    시크릿은 S3 에 올리지 않는다 — 기존대로 SSM Parameter Store.
# ─────────────────────────────────────────────────────────────────────────
log "7/8 config S3 업로드 (s3://${CONFIG_BUCKET}/${CONFIG_PREFIX}/) + user-data 렌더링"

# 7a. 버킷 생성 + role 의 config-bucket-read 권한 (일회성 셋업 스크립트에 위임,
#     멱등). 위 1단계에서 role 을 이미 만들었으므로 정책이 붙는다.
AWS_REGION="${REGION}" ROLE_NAME="${ROLE_NAME}" \
    CONFIG_BUCKET="${CONFIG_BUCKET}" CONFIG_PREFIX="${CONFIG_PREFIX}" \
    "${SCRIPT_DIR}/setup-s3-config.sh"

# 7b. config 디렉토리를 S3 로 sync. EC2 디렉토리 구조(infra/...)를 그대로 보존.
upload_config() {  # $1=로컬상대경로  $2=S3 키
    aws s3 cp "${REPO_ROOT}/infra/$1" "s3://${CONFIG_BUCKET}/${CONFIG_PREFIX}/$2" >/dev/null
}
upload_config compose/compose.aws-mvp.yml compose/compose.aws-mvp.yml
upload_config env/aws-mvp.env             env/aws-mvp.env
upload_config caddy/Caddyfile             caddy/Caddyfile
upload_config litellm/config.yaml         litellm/config.yaml
upload_config litellm/strip_history.py    litellm/strip_history.py
upload_config searxng/settings.yml        searxng/settings.yml
log "    config 6개 업로드 완료"

# 7c. user-data 렌더링 — 이제 config 를 inline 하지 않고 placeholder 4개만 치환.
USERDATA_FILE="${STATE_DIR}/user-data.rendered.sh"
sed -e "s|@@AWS_REGION@@|${REGION}|g" \
    -e "s|@@ECR_REGISTRY@@|${ECR_REG}|g" \
    -e "s|@@CONFIG_BUCKET@@|${CONFIG_BUCKET}|g" \
    -e "s|@@CONFIG_PREFIX@@|${CONFIG_PREFIX}|g" \
    "${SCRIPT_DIR}/user-data.sh" > "${USERDATA_FILE}"

# user-data 크기 확인. config 가 빠졌으므로 셸 본문(~7KB)만 남아 16KB 한도에
# 여유롭게 들어간다. file:// 로 넘기면 CLI 가 base64 인코딩(25600 한도)한다.
USERDATA_B64_SIZE=$(base64 -w0 < "${USERDATA_FILE}" | wc -c)
log "    user-data base64: ${USERDATA_B64_SIZE} bytes (AWS limit 25600)"
if [ "${USERDATA_B64_SIZE}" -gt 25600 ]; then
    fail "user-data base64 가 ${USERDATA_B64_SIZE} bytes 로 25600 한도 초과 (config 는 S3 라 셸 본문 문제 — user-data.sh 를 줄여라)"
fi

# ─────────────────────────────────────────────────────────────────────────
# 8. EC2 launch + EBS data volume + EIP associate
# ─────────────────────────────────────────────────────────────────────────
log "8/8 EC2 launch (${INSTANCE_TYPE}, data vol ${DATA_VOLUME_SIZE_GB}GB)"
INSTANCE_ID=$(aws ec2 run-instances --region "${REGION}" \
    --image-id "${AMI_ID}" \
    --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_NAME}" \
    --security-group-ids "${SG_ID}" \
    --subnet-id "${SUBNET_ID}" \
    --iam-instance-profile "Name=${PROFILE_NAME}" \
    --user-data "file://${USERDATA_FILE}" \
    --block-device-mappings "[
      {\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":20,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}},
      {\"DeviceName\":\"/dev/sdf\",\"Ebs\":{\"VolumeSize\":${DATA_VOLUME_SIZE_GB},\"VolumeType\":\"gp3\",\"DeleteOnTermination\":false}}
    ]" \
    --tag-specifications "ResourceType=instance,Tags=[
      {Key=Project,Value=${PROJECT_TAG}},
      {Key=Name,Value=rx-agent-frontend}
    ]" "ResourceType=volume,Tags=[{Key=Project,Value=${PROJECT_TAG}}]" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --query 'Instances[0].InstanceId' --output text)

echo "${INSTANCE_ID}" > "${STATE_DIR}/instance-id"
log "    InstanceId=${INSTANCE_ID}"

log "    running 상태 대기..."
aws ec2 wait instance-running --region "${REGION}" --instance-ids "${INSTANCE_ID}"

log "    EIP 연결"
aws ec2 associate-address --region "${REGION}" \
  --allocation-id "${EIP_ALLOC}" --instance-id "${INSTANCE_ID}" >/dev/null

# ─────────────────────────────────────────────────────────────────────────
# 결과 요약
# ─────────────────────────────────────────────────────────────────────────
cat <<EOF

╔═══════════════════════════════════════════════════════════════════════╗
║ AWS frontend EC2 셋업 완료                                            ║
╠═══════════════════════════════════════════════════════════════════════╣
║ InstanceId : ${INSTANCE_ID}
║ EIP        : ${EIP}
║ AMI        : ${AMI_ID}
║ SG         : ${SG_ID}
║ Region     : ${REGION}
║ State files: ${STATE_DIR}/
╠═══════════════════════════════════════════════════════════════════════╣
║ 다음 단계:                                                            ║
║   1. 회사 도메인 등록기관에 A 레코드 추가:                            ║
║        agent.<your-domain>   A   ${EIP}                               ║
║   2. SSM 시크릿 5개 등록되어 있는지 확인:                             ║
║        make aws-secrets-put                                           ║
║   3. user-data 가 끝나길 기다린 후 (~3분):                            ║
║        make aws-status                                                ║
║        make aws-logs                                                  ║
║   4. 첫 이미지 배포:                                                  ║
║        make aws-deploy                                                ║
║   5. 브라우저에서 https://agent.<your-domain> 접속                    ║
╚═══════════════════════════════════════════════════════════════════════╝
EOF
