#!/usr/bin/env bash
# 일회성 — config 운반용 S3 버킷 + EC2 role 의 read 권한을 프로비저닝한다.
#
# 왜 별도 스크립트인가:
#   - 버킷 생성/role 정책은 *한 번만* 필요한 셋업이다. 반복 실행되는 deploy.sh
#     에 두면 매 배포마다 IAM 변경 권한이 필요하고 책임이 섞인다.
#   - setup-ec2.sh 는 instance-id 가드로 재실행이 막혀 있어, 이미 배포된
#     인스턴스에 "S3 셋업만 추가"할 자리가 없다 → 이 스크립트가 그 자리.
#   모두 멱등(head||create, put-role-policy 덮어쓰기)이라 여러 번 실행해도 안전.
#
# 환경변수:
#   AWS_REGION (필수)
#
# 사용:
#   make aws-setup-s3            # 이미 배포된 인스턴스에 권한+버킷 추가
#   (setup-ec2.sh 가 신규 인스턴스 프로비저닝 중 내부적으로도 호출)

set -euo pipefail

REGION="${AWS_REGION:?AWS_REGION required}"
ROLE_NAME="${ROLE_NAME:-rx-agent-frontend-ec2}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CONFIG_BUCKET="${CONFIG_BUCKET:-rx-agent-frontend-config-${ACCOUNT_ID}}"
CONFIG_PREFIX="${CONFIG_PREFIX:-aws-mvp}"

log()  { printf '\033[1;34m[setup-s3]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[setup-s3]\033[0m %s\n' "$*" >&2; exit 1; }

aws sts get-caller-identity >/dev/null || fail "aws cli 자격증명 실패."

# 1. 버킷 멱등 생성 (region 별 LocationConstraint 처리).
if aws s3api head-bucket --bucket "${CONFIG_BUCKET}" >/dev/null 2>&1; then
    log "버킷 이미 존재: ${CONFIG_BUCKET}"
else
    log "버킷 생성: ${CONFIG_BUCKET}"
    if [ "${REGION}" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "${CONFIG_BUCKET}" --region "${REGION}" >/dev/null
    else
        aws s3api create-bucket --bucket "${CONFIG_BUCKET}" --region "${REGION}" \
            --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
    fi
    # public access 전면 차단 + 버저닝(config 롤백용).
    aws s3api put-public-access-block --bucket "${CONFIG_BUCKET}" \
        --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-versioning --bucket "${CONFIG_BUCKET}" \
        --versioning-configuration Status=Enabled
fi

# 2. EC2 role 에 config 버킷 read 권한. role 이 없으면(=setup-ec2 미실행) 경고만.
#    put-role-policy 는 같은 이름이면 덮어쓴다 — 기존 인스턴스 role 에도 안전 적용.
if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
    log "role 정책 적용: ${ROLE_NAME} / config-bucket-read"
    aws iam put-role-policy --role-name "${ROLE_NAME}" \
      --policy-name config-bucket-read \
      --policy-document "{
        \"Version\":\"2012-10-17\",
        \"Statement\":[{
          \"Effect\":\"Allow\",
          \"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],
          \"Resource\":[
            \"arn:aws:s3:::${CONFIG_BUCKET}\",
            \"arn:aws:s3:::${CONFIG_BUCKET}/${CONFIG_PREFIX}/*\"
          ]
        }]
      }"
else
    log "경고: role ${ROLE_NAME} 없음. setup-ec2.sh 가 먼저 role 을 만든 뒤 다시 실행하라."
fi

log "완료. config 버킷=${CONFIG_BUCKET}, prefix=${CONFIG_PREFIX}"
