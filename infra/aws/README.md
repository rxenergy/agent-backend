# AWS MVP Frontend Deployment

사내 MVP 용 OpenWebUI(Frontend) 를 AWS EC2 단일 인스턴스에 배포하는 절차.
백엔드(Agent API / vLLM / OpenSearch / Postgres) 는 온프레미스 GPU 서버에 그대로
유지하고, 프론트엔드만 AWS 로 끌어내 Tailscale mesh VPN 으로 온프레와 연결한다.

> IaC(Terraform/CDK) 는 사용하지 않는다 — MVP 규모(EC2 1대 + EBS 1개 + EIP 1개)
> 에 IaC 의 ROI 가 없다. 대신 1회성 인프라 생성도 셸 스크립트(`setup-ec2.sh`) 로
> 재현 가능하게 만들고, 반복 작업(이미지 빌드/푸시/재배포) 은 Makefile + SSM
> Send-Command 로 무인화한다.
>
> **EC2 는 Git 을 사용하지 않는다.** config 파일(`compose.aws-mvp.yml`,
> `aws-mvp.env`, `Caddyfile`, `litellm/{config.yaml,strip_history.py}`,
> `searxng/settings.yml`) 은 S3 버킷(`rx-agent-frontend-config-<account>` /
> prefix `aws-mvp/`)으로 운반한다. `setup-ec2.sh`/`deploy.sh` 가 로컬→S3 업로드,
> EC2 는 부팅·재배포 시 `aws s3 sync` 로 `/opt/agent-saas/infra/` 를 재현한다
> (인스턴스 role 의 `config-bucket-read` 권한). 과거 base64-user-data-inline 방식은
> 16KB 한도에 부딪혀 S3 로 전환했다. **시크릿은 S3 가 아니라 SSM Parameter Store**
> (`/rx-agent/frontend/*`)에 남는다. 로컬 ↔ EC2 동기화는 `make aws-deploy` 가 책임진다.

## 토폴로지

```
[user laptop]──TLS──> agent.<회사도메인> (등록기관 A 레코드)
                       │
                       ▼
            ┌─── EC2 t3.small (ap-northeast-2) ───────────┐
            │  Caddy :443/:80  → reverse proxy            │
            │      open-webui :8080 (ECR image)           │
            │       OPENAI_API_BASE_URLS (공존 2개):      │
            │        [0] http://onprem-agent:8000/v1 ◀──┐ │
            │        [1] http://litellm:4000/v1 ──┐     │ │
            │      litellm :4000 (Bedrock 변환) ◀──┘     │ │
            │           └──HTTPS(bearer)──> Bedrock      │ │
            │  tailscaled (hostname=aws-frontend)       │ │
            │  /data (EBS gp3 20GB)                     │ │
            └───────────────────────────────────────────┼─┘
                                                        │ tailnet
            ┌── 온프레미스 GPU 서버 ────────────────────┼─┐
            │  tailscaled (hostname=onprem-agent)       ◀┘
            │  compose.onprem.yml: agent-api:8000, vllm,..│
            └─────────────────────────────────────────────┘
```

OpenWebUI 모델 드롭다운에 백엔드 2개가 *공존* 노출된다:
- **온프레 agent-api** (SMR 에이전트 — RAG/검증/재현성 이벤트 경유)
- **litellm → Bedrock** (raw Claude passthrough — 에이전트 로직 미경유, fallback/비교용)

⚠ litellm 경로는 사용자 입력이 raw 로 AWS Bedrock 으로 송신된다(재현성 이벤트
없음). 재현 가능한 실행은 온프레 agent-api connection 에서만 보장된다.

## 파일 구성

```
infra/aws/
├── README.md           ← 이 파일
├── setup-ec2.sh        ← 1회성 인프라 생성 (EC2/EBS/EIP/SG/IAM, 내부에서 setup-s3-config 호출)
├── setup-s3-config.sh  ← 1회성 config S3 버킷 + EC2 role read 권한 (이미 배포된 인스턴스용)
├── user-data.sh        ← EC2 부팅 시 자동 실행 (setup-ec2.sh 가 placeholder 치환)
├── deploy.sh           ← config S3 업로드 + SSM Send-Command 로 sync + 컨테이너 재배포
├── secrets-put.sh      ← SSM Parameter Store 시크릿 등록
├── destroy.sh          ← 리소스 일괄 삭제
└── .state/             ← 생성된 ID 캐시 (gitignore — InstanceId/EIP/PEM)
```

`infra/compose/compose.aws-mvp.yml`, `infra/env/aws-mvp.env`, `infra/caddy/Caddyfile`,
`infra/litellm/{config.yaml,strip_history.py}`, `infra/searxng/settings.yml` 은
S3 를 거쳐 EC2 위에서 컨테이너 런타임이 사용한다.

## 사전 준비

1. **AWS CLI v2** 설치, `aws configure` 또는 SSO 로 자격증명 설정
2. **IAM 권한**: `rx-agent-mvp-deployer-policy` (또는 AdministratorAccess) 부착
3. **회사 도메인 등록기관** 접근 권한 (A 레코드 추가용)
4. **Tailscale 어드민**: reusable auth key 발급 (tag:frontend-aws), ACL 설정
5. **온프레미스**: tailscaled 설치, hostname=`onprem-agent`, agent-api:8000 도달 가능

## 절차 (처음 배포)

### 1. 시크릿 등록
```bash
make aws-secrets-put
# WEBUI_SECRET_KEY:   자동 생성
# OPENAI_API_KEY:     온프레 agent-api 토큰 입력 (검증 안 하면 'dummy')
# Tailscale authkey:  Tailscale 어드민에서 발급한 키 입력
# bedrock_api_key:    Bedrock 임시 API 키(bearer token) 입력
# litellm_master_key: 자동 생성
```

> **Bedrock 모델 ID 확인**: `infra/litellm/config.yaml` 의 inference-profile ID 는
> 배포 region 에서 ACTIVE 한 것이어야 한다. 추측 금지 —
> `aws bedrock list-inference-profiles --region ap-northeast-2` 로 확인해 박는다.

### 2. ECR 레포 + EC2 + EBS + EIP + SG + IAM Role 생성
```bash
make aws-setup
```
출력의 **EIP** 를 메모.

### 3. 회사 도메인 등록기관에 A 레코드 추가
```
Type:  A
Host:  agent
Value: <위에서 메모한 EIP>
TTL:   300
```
가비아·카페24·Cloudflare 등 어디든 동일.

### 4. user-data 완료 대기 (약 3분)
```bash
make aws-status      # State=running 확인
make aws-ssh         # SSM 세션으로 들어가서 tail -f /var/log/user-data.log
```

### 5. 첫 이미지 빌드·푸시·배포
```bash
make aws-deploy
# = aws-build → aws-push → SSM 으로 docker pull + compose up
```

### 6. 브라우저 확인
```
https://agent.<회사도메인>
```
Caddy 가 Let's Encrypt 인증서 자동 발급 (첫 요청 시 ~10초 지연).

## 일상 운영

| 작업 | 명령 |
|---|---|
| 코드 수정 후 재배포 | `make aws-deploy` (1~2분) |
| 컨테이너 로그 확인 | `make aws-logs` |
| EC2 접속 (셸) | `make aws-ssh` |
| 인스턴스 상태 | `make aws-status` |
| 시크릿 회전 | `aws ssm put-parameter --name /rx-agent/frontend/... --overwrite` 후 `make aws-deploy` |
| 인스턴스 재시작 | `aws ec2 reboot-instances --instance-ids $(cat infra/aws/.state/instance-id)` |
| 전체 삭제 | `make aws-destroy` |

## SSM 권한이 막힐 때 — break-glass SSH

`setup-ec2.sh` 가 `rx-agent-frontend-key.pem` 을 `.state/` 에 저장한다.
SSM Agent 가 죽었을 때만 사용:

```bash
ssh -i infra/aws/.state/rx-agent-frontend-key.pem ec2-user@<EIP>
```

평시엔 SSH 22 포트를 SG 에서 닫는 게 안전:
```bash
aws ec2 revoke-security-group-ingress \
  --group-id <SG_ID> --protocol tcp --port 22 --cidr 0.0.0.0/0
```

## 트러블슈팅

### user-data 가 끝나지 않음
```bash
make aws-ssh
sudo tail -f /var/log/user-data.log
sudo tail -f /var/log/cloud-init-output.log
```

### Tailscale 연결 실패
```bash
sudo tailscale status
sudo tailscale ping onprem-agent
# 실패 시 auth key 만료 / ACL / 온프레측 tailscaled 상태 점검
```

### Caddy TLS 발급 실패
```bash
sudo docker logs caddy
```
- A 레코드 전파 미완료 (TTL 대기)
- 80 포트 차단 (SG 확인)
- ACME rate limit (도메인당 주당 50회 — MVP 에선 거의 안 걸림)

### OpenWebUI 가 백엔드 모델을 못 봄
```bash
make aws-ssh
# [0] 온프레 agent-api
docker exec open-webui curl -sf http://onprem-agent:8000/v1/models
# 실패 → tailnet 문제. 성공인데 UI 에서 안 보이면 OPENAI_API_KEYS[0] 토큰 불일치.
```

### Bedrock(litellm) 모델이 안 보임
```bash
make aws-ssh
docker logs litellm
# [1] litellm → Bedrock. master key 로 모델 목록 확인:
docker exec litellm curl -sf -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  http://localhost:4000/v1/models
# - 인증 401: AWS_BEARER_TOKEN_BEDROCK 만료/오타 (SSM bedrock_api_key 점검)
# - 모델 없음/AccessDenied: config.yaml inference-profile ID 가 region 에서 미가용
#   (aws bedrock list-inference-profiles 로 ACTIVE ID 확인)
# - UI 에 안 뜸: OPENAI_API_KEYS[1] 이 litellm_master_key 와 불일치
```

### 데이터 백업
```bash
# SQLite DB 백업
aws ssm send-command --document-name AWS-RunShellScript \
  --instance-ids $(cat infra/aws/.state/instance-id) \
  --parameters 'commands=["sqlite3 /data/open-webui/webui.db .dump | gzip > /data/backup-$(date +%F).sql.gz"]'

# EBS 스냅샷 (DLM 정책 권장 — AWS 콘솔에서 1회 설정)
aws ec2 create-snapshot \
  --volume-id <data-volume-id> \
  --description "rx-agent-frontend-$(date +%F)"
```

## 비용 (대략, 서울 리전)

| 항목 | 월 |
|---|---|
| EC2 t3.large 24/7 (8GB RAM) | ~$60 |
| EBS gp3 40GB | $3.5 |
| EIP (부착 중) | $0 |
| 데이터 전송 | ~$1 |
| ECR 스토리지 (이미지 5개) | <$1 |
| Bedrock (사용량 기반, litellm 경유) | 호출량 비례 (별도) |
| **합계** | **~$65 / 월 + Bedrock 사용량** |

> litellm 컨테이너는 같은 인스턴스 안에서 OpenWebUI 와 함께 돈다(추가 인스턴스 없음).
> t3.large(8GB)는 컨테이너 3개(open-webui+caddy+litellm)에 충분한 RAM 여유를 둔다.
> 비용을 줄이려면 t3.medium(4GB, ~$30/월)도 가능하나, swap 의존이 남는다.
> Bedrock 비용만 토큰 사용량에 비례해 별도 과금된다.

DNS 는 회사 도메인 등록기관에서 무료, Tailscale 은 Free plan (3 user/100 device 이하).
