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

## Preflight

`OpenSearchPreflight.required_fields` 는 인덱스 매핑에 특정 필드 존재를 강제한다.
v3.1 변형이 활성화되고 **동시에** 대상 인덱스가 v2 일 때만 4필드를 요구한다
(`config/profiles.py`). v1 을 가리키는 동안에는 요구하지 않아 부팅에 영향이 없다.
