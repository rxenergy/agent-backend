# OpenSearch Index Mappings

| 파일 | 상태 | 비고 |
|------|------|------|
| `nrc-all-v1.json` | **active** | 현재 적재된 RAG 데이터의 스키마. `init.sh` / `OPENSEARCH_INDEX` / `settings.opensearch_index` 기본값이 모두 이것. NRC ADAMS/govinfo (rich `doc_metadata`). |
| `nrc-all-v2.json` | **planned (미적재)** | v1 + v3.1 규제 메타 4필드(`clause_id`, `authority_tier`, `jurisdiction`, `effective_on`). 아직 색인된 데이터 없음. |
| ~~`nrc-all-v3.json`~~ | **removed** | 구(舊) 데이터 형식. v3 → v1 로 업데이트되어 더 이상 사용하지 않음 (레거시 삭제). |

## v1 → v2 전환이 필요한 이유

v2 의 4개 필드는 Node 6 (`retrieval_evaluate`) 의 **G3 Regulatory Structural** 신호
(`clause_id_resolves`, `authority_tier ≥ secondary` hard gate, `jurisdiction_match`,
`version_match`) 입력이다.

strict 매핑에 필드를 *추가*하는 것 자체는 재색인 없이 가능(add-only)하지만,
**기존 v1 문서에는 이 필드 값이 존재하지 않는다** (null). 따라서 G3 신호가 실제
값을 가지려면 **코퍼스 재적재(re-ingest)** 가 선행되어야 한다.

이 때문에:

- **v1 스키마는 동결** — 기존 적재 데이터를 보호.
- **v2 는 예정 스펙** — 재적재 준비가 되면 `OPENSEARCH_INDEX=nrc-all-v2` +
  `nrc-all-v2.json` 으로 새 인덱스를 생성/적재.
- **RAG 코드는 v1 기준으로 동작** — `retriever_opensearch._hit_to_chunk` 는 4필드를
  *있으면 읽고 없으면 None*. 단 `authority_tier` 는 v1 의 `collection` 값에서
  read-time 유도(`_derive_authority_tier`)하므로 v1 에서도 부분 동작한다.
  `clause_id` / `jurisdiction` / `effective_on` 은 v2 적재 전까지 None.

## "v2 사용 가능" 판단 — 단일 출처

판단 기준은 **인덱스 이름이 아니라** 선언적 설정
`OPENSEARCH_SCHEMA_VERSION`(`settings.opensearch_schema_version`, 기본 `v1`)이다.
이름은 임의(`nrc-smr-2026` 등)일 수 있어 능력 판단을 네이밍 관습에 의존시키지
않는다.

- **읽기 경로** (`_hit_to_chunk`): 스키마 버전과 무관하게 4필드를 *있으면 읽고
  없으면 None*. `authority_tier` 만 v1 의 `collection` 에서 유도. → v1/v2 모두 안전.
- **Preflight** (`OpenSearchPreflight.required_fields`, `config/profiles.py`):
  v3.1 변형 활성화 **AND** `opensearch_schema_version == "v2"` 일 때만 매핑에
  4필드 존재를 강제. v1 선언 시에는 요구하지 않아 부팅 무영향.
- **PR-5 G3** (예정): `opensearch_schema_version` 을 읽어 신뢰할 신호를 결정.
  v1 → `authority_tier` 만, `clause_id`/`jurisdiction`/`effective_on` 은
  "unknown(skip)". v2 → 전 신호 신뢰.

### 잔여 한계 (정직히 기록)

`opensearch_schema_version="v2"` + preflight 필드 존재 확인은 **매핑 수준**
전제만 검증한다. *모든 문서가 실제로 값을 가졌는가*는 운영자가 재적재로
보장한다고 **선언**하는 것이며, 시스템이 문서를 샘플링해 검증하지는 않는다
(per-doc 샘플링 + 매핑 `_meta.schema_version` 교차확인은 향후 hardening 후보).
