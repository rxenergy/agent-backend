# vLLM 스택 (Gemma 4 26B A4B-it, OpenAI 호환 API)

검색 스택과 독립적으로 운용되는 vLLM 서빙 모듈. 호스트는 NVIDIA DGX Spark
(GB10 / Grace Blackwell, ARM64, CUDA 13) 환경을 가정한다.

vLLM 공식 Docker Hub 이미지는 x86_64 전용이므로,
[eugr/spark-vllm-docker](https://github.com/eugr/spark-vllm-docker) 의
빌드 산출물(`vllm-node:latest`)을 그대로 재사용한다.

## 1. vLLM 이미지 빌드 (최초 1회)

빌드 위치는 **본 프로젝트 바깥**을 권장한다. spark-vllm-docker는 업스트림이
활발히 갱신되는 외부 도구라 본 레포에 포함시키지 않고, 빌드 산출물인
docker 이미지 태그(`vllm-node:latest`)만 호스트 docker 데몬에서 공유해 쓴다.
빌드가 끝나면 클론 디렉터리는 지워도 무방하다.

```bash
cd ~/rx-git                                       # agent-backend의 형제 위치
git clone https://github.com/eugr/spark-vllm-docker.git
cd spark-vllm-docker
./build-and-copy.sh
docker images | grep vllm-node                    # vllm-node:latest 확인
```

기본 빌드는 `vllm-node:latest` 태그를 만든다. 다른 옵션을 쓰면 태그가 달라지므로
`../docker/vllm/docker-compose.yml`의 `image:` 줄도 함께 바꿔야 한다.

| 빌드 명령 | 태그 |
| --- | --- |
| `./build-and-copy.sh` | `vllm-node:latest` |
| `./build-and-copy.sh --tf5` | `vllm-node-tf5:latest` |
| `./build-and-copy.sh --exp-mxfp4` | `vllm-node-mxfp4:latest` |

## 2. 기동

프로젝트 루트에서:

```bash
docker compose -f docker/vllm/docker-compose.yml up -d
docker logs -f agent-backend-vllm    # "Started server" 로그까지 대기
```

첫 로딩은 모델 크기상 1~3분 가량 걸린다. healthcheck `start_period`는 180초.

## 3. 검증

```bash
curl -fsS http://localhost:8001/v1/models | jq

curl http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemma-4-26b-a4b-it",
    "messages": [{"role": "user", "content": "안녕"}],
    "max_tokens": 64
  }'
```

## 4. 주요 파라미터 (`docker-compose.yml` `command:`)

| 옵션 | 값 | 설명 |
| --- | --- | --- |
| `--max-model-len` | `32768` | 컨텍스트 길이 상한 (모델 기본 256K, KV 캐시 절약 위해 32K로 시작) |
| `--max-num-batched-tokens` | `4096` | Gemma 4는 멀티모달이라 이미지 1개당 최대 2496 토큰. 기본값 2048보다 커야 부팅됨 |
| `--gpu-memory-utilization` | `0.7` | 단일 GPU에서 검색 스택과 공존 여지를 둔다. OOM 시 `0.55`로 낮춤 |
| `--load-format` | `fastsafetensors` | spark-vllm-docker 권장 (다중 스레드 로딩) |
| `--served-model-name` | `gemma-4-26b-a4b-it` | OpenAI API의 `model` 필드로 노출되는 별칭 |

> Attention backend는 Gemma 4의 head_dim이 heterogeneous(256/512)라 vLLM이 자동으로
> `TRITON_ATTN`을 강제한다. 별도 env로 지정하지 않는다.

## 5. 중지

```bash
docker compose -f docker/vllm/docker-compose.yml down
```
