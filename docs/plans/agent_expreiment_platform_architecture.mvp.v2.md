
---

# SMR 인허가 Agent MVP 개발·실험 플랫폼 아키텍처

> Agent 기능 기획을 실제 개발, 테스트, 배포, 운영, 개선 가능한 시스템으로 전환하기 위한 아키텍처 정의

---

## 1. 문서 목적

본 문서는 SMR 인허가 Agent의 기능 기획을 실제 개발 가능한 시스템으로 구현하기 위한 **MVP 개발·실험 플랫폼 아키텍처**를 정의한다.

기획 문서는 Agent를 사용자 질의 이해, 데이터 접근, 답변 생성, 답변 검증, 사용자 피드백 처리로 구성되는 오케스트레이션 레이어로 정의하고 있으며, workflow는 Intent Classification → Retrieval → Generation → Verification → Response 순서로 구성된다. 

본 문서는 해당 기획을 실제 개발 가능한 시스템으로 만들기 위해 다음을 정의한다.

```text
- Agent Runtime
- Tool Integration
- Prompt Management
- Context Management
- Long-term Memory Management
- Observability / Trace
- Evaluation
- Artifact Store
- Deployment Profile
```

---

## 2. 설계 대상

본 문서의 설계 대상은 완성된 상용 Agent 서비스가 아니다.

본 문서의 설계 대상은 다음이다.

```text
Agent를 개발·테스트·배포·운영·개선하기 위한
MVP Agent Experiment Platform
```

즉, 이 시스템은 다음을 가능하게 해야 한다.

```text
Agent Backend를 실행하고,
Agent workflow를 실험 가능하게 관리하고,
Tool 호출을 명시적으로 통제하고,
Prompt와 Context를 재현 가능하게 기록하고,
Session Memory로 multi-turn context를 관리하고,
Memory Candidate를 expert review를 통해 검증하며,
Approved Domain Memory만 장기 지식으로 재사용하고,
평가 Dataset으로 회귀 테스트하며,
AWS와 On-premises 환경을 같은 Docker 구조로 유지한다.
```

기존 아키텍처 초안도 MVP의 목표를 운영 안정성이 아니라 Agent 가능성 검증으로 정의하고, interaction log, trace, prompt version, context snapshot, eval dataset, Docker 기반 AWS/온프레미스 이전 가능성을 핵심 요소로 제시한다. 

---

## 3. 설계 범위

### 3.1 포함 범위

| 영역                 | 포함 항목                                             |
| ------------------ | ------------------------------------------------- |
| Agent Backend      | FastAPI 기반 Agent API                              |
| Agent Runtime      | `AgentRunner`, workflow variant                   |
| Tool Integration   | `ToolRegistry`, `ToolExecutor`, tool schema       |
| Prompt Management  | Git prompt registry + Phoenix                     |
| Context Management | `ContextPack`, `context_hash`, `context_snapshot` |
| Session Memory     | recent turns, summary, active entities            |
| Long-term Memory   | memory candidate, approved memory, stale memory   |
| Expert Review      | memory candidate 승인/거절/폐기                         |
| Evaluation         | pytest, Ragas, Promptfoo, Phoenix experiment      |
| Observability      | OpenTelemetry, Phoenix, Grafana, Tempo, Loki      |
| State DB           | Postgres + pgvector                               |
| Artifact Store     | S3 / MinIO                                        |
| Deployment         | Docker Compose profiles                           |
| AWS MVP            | EC2 + Docker Compose + S3                         |
| On-premises        | VM/bare metal + Docker Compose + MinIO            |

### 3.2 제외 범위

| 제외 항목                            | 제외 이유                |
| -------------------------------- | -------------------- |
| 모델 서빙 / vLLM                     | 별도 인프라               |
| OpenSearch / RAG DB 상세 구성        | 별도 검색 인프라            |
| Kubernetes                       | MVP 단계에서는 복잡도 증가     |
| Kafka / Airflow                  | 초기 event volume에는 과함 |
| 운영용 HA / Autoscaling             | MVP 목표가 아님           |
| 자동 self-learning                 | 원자력 도메인에서 위험         |
| 검토 없는 long-term memory injection | 오염된 memory 누적 가능     |

---

## 4. 핵심 설계 원칙

### 4.1 Agent는 고정 서비스가 아니라 실험 가능한 workflow다

```text
AGENT_VARIANT=fake_echo_v0
AGENT_VARIANT=sequential_verified_rag_v1
AGENT_VARIANT=langgraph_verified_rag_v1
```

초기 MVP에서는 `sequential_verified_rag_v1`을 기본으로 둔다.
LangGraph는 retry edge, tool orchestration, checkpoint, multi-agent 구조가 실제로 필요해질 때 variant로 도입한다.

---

### 4.2 Tool 호출은 Agent Runtime이 통제한다

MVP에서는 LLM이 자유롭게 tool을 선택하는 구조를 기본값으로 두지 않는다.

```text
AgentRunner
→ Workflow Node
→ ToolExecutor
→ Tool Adapter
```

즉, Tool은 LLM의 자율 실행 대상이 아니라 **Agent Runtime이 통제하는 capability**다.

---

### 4.3 Context와 Memory를 분리한다

```text
Context = 현재 답변 생성을 위한 단기 입력
Memory = 여러 실행과 평가 사이클을 거쳐 재사용되는 장기 상태 또는 개선 자산
```

`ContextSnapshot`은 Memory가 아니라 **재현성 artifact**다.

---

### 4.4 대화 내용은 자동으로 장기 지식이 되지 않는다

```text
Interaction
→ Memory Candidate
→ Expert Review
→ Approved Domain Memory / Golden Dataset / Rejected Archive
```

검토되지 않은 memory는 답변에 주입하지 않는다.

---

### 4.5 모든 실행은 재현 가능해야 한다

하나의 답변은 다음 정보로 재현 가능해야 한다.

```text
interaction_id
trace_id
agent_variant
scenario_object
scenario_depth
prompt_profile_id
prompt_version
rendered_prompt_hash
context_hash
retrieved_chunk_ids
tool_result_refs
memory_ids_used
verification_result
model_options
```

---

## 5. 전체 시스템 아키텍처

```text
┌──────────────────────────────────────────────────────────────┐
│                         Client                               │
│              OpenWebUI / API Client / Eval Runner            │
└───────────────────────────────┬──────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                        agent-api                              │
│                                                              │
│  FastAPI                                                     │
│  OpenAI-compatible API                                       │
│                                                              │
│  AgentRunner                                                 │
│  ScenarioRouter                                              │
│  ToolExecutor                                                │
│  PromptResolver                                              │
│  ContextBuilder                                              │
│  MemoryResolver                                              │
│  VerificationPolicy                                          │
│  EventRecorder                                               │
└───────────────┬───────────────┬────────────────┬─────────────┘
                │               │                │
                │ OTLP          │ artifacts      │ state / memory
                ▼               ▼                ▼
┌──────────────────────────┐   ┌────────────────────────────────┐
│ OpenTelemetry Collector  │   │ Artifact Store                  │
│ - traces                 │   │ AWS: S3                         │
│ - metrics                │   │ On-prem: MinIO                  │
│ - logs                   │   │                                │
└──────────────┬───────────┘   │ - interaction_events            │
               │               │ - context_snapshots             │
               │               │ - prompt_render_records         │
               │               │ - tool_result_records           │
               │               │ - eval_runs                     │
               │               │ - memory_artifacts              │
               │               └────────────────────────────────┘
               ▼
┌──────────────────────────┐
│ Phoenix                  │
│ - agent traces           │
│ - prompt versions        │
│ - datasets               │
│ - experiments            │
│ - eval results           │
└──────────────┬───────────┘
               │
               ▼
┌──────────────────────────┐
│ Grafana Stack            │
│ - Grafana                │
│ - Tempo                  │
│ - Prometheus             │
│ - Loki                   │
└──────────────────────────┘


┌──────────────────────────────────────────────────────────────┐
│                         State DB                              │
│                         Postgres + pgvector                   │
│ - session_memory                                             │
│ - memory_candidates                                          │
│ - approved_memories                                          │
│ - expert_reviews                                             │
│ - dataset_candidates                                         │
│ - tool_call_records                                          │
└──────────────────────────────────────────────────────────────┘
```

---

## 6. 기술 스택

| 계층              | 기술                                                                   |
| --------------- | -------------------------------------------------------------------- |
| Backend         | Python 3.12, FastAPI, Pydantic v2, Uvicorn                           |
| Agent Runtime   | `AgentRunner`, `AgentVariant`, `WorkflowNode`                        |
| Tool Runtime    | `ToolRegistry`, `ToolExecutor`, `ToolExecutionContext`, `ToolResult` |
| Prompt          | Git prompt registry, Phoenix prompt management                       |
| Context         | `ContextPack`, context snapshot, context hash                        |
| Memory          | Postgres, pgvector, optional Qdrant                                  |
| Observability   | OpenTelemetry, Phoenix, Tempo, Grafana, Prometheus, Loki             |
| Evaluation      | pytest, Ragas, Promptfoo, Phoenix experiment                         |
| Artifact        | S3 / MinIO                                                           |
| Deployment      | Dockerfile, Docker Compose profiles                                  |
| Local / On-prem | MinIO, Postgres, pgvector                                            |
| AWS MVP         | EC2, Docker Compose, S3                                              |

---

## 7. Agent Runtime Architecture

```text
agent-api
└─ application
   ├─ AgentRunner
   ├─ ScenarioRouter
   ├─ ToolExecutor
   ├─ PromptResolver
   ├─ ContextBuilder
   ├─ MemoryResolver
   ├─ VerificationPolicy
   └─ EventRecorder
```

### 7.1 기본 Workflow

```text
1. intent_classification
2. scenario_routing
3. tool.memory.session_load
4. tool.retriever.search
5. tool.memory.approved_search
6. context_building
7. prompt_rendering
8. generation
9. tool.document.resolve_citation
10. tool.verification.citation_check
11. tool.verification.faithfulness_check
12. memory_candidate_extract
13. tool.memory.session_update
14. tool.artifact.write_event
15. response_formatting
```

---

## 8. Tool Integration Architecture

## 8.1 Tool의 정의

본 아키텍처에서 Tool은 다음 조건을 만족하는 실행 단위다.

```text
1. 명시적인 name과 version을 가진다.
2. input schema와 output schema가 정의되어 있다.
3. Agent workflow에서 호출 가능하다.
4. 호출 결과가 trace에 기록된다.
5. 실패 시 표준화된 error를 반환한다.
6. local / AWS MVP / on-prem 환경에서 동일 interface로 동작한다.
```

---

## 8.2 Tool 분류

| Tool Type         | 설명                           | 예시                                              |
| ----------------- | ---------------------------- | ----------------------------------------------- |
| Retrieval Tool    | 문서, chunk, metadata 검색       | `retriever.search`                              |
| Document Tool     | citation, page, section 조회   | `document.resolve_citation`                     |
| Memory Tool       | session / approved memory 조회 | `memory.session_load`, `memory.approved_search` |
| Verification Tool | citation, faithfulness 검증    | `verification.citation_check`                   |
| Dataset Tool      | 실패 사례, 평가 후보 생성              | `dataset.candidate_create`                      |
| Artifact Tool     | event, snapshot 저장           | `artifact.write_event`                          |
| Utility Tool      | 날짜, 단위, deterministic 처리     | `date_normalize`, `unit_convert`                |

---

## 8.3 MVP Tool 목록

| Tool                              | 목적                                        | Required |
| --------------------------------- | ----------------------------------------- | -------: |
| `retriever.search`                | 사용자 질의와 entity 기반 문서 검색                   |      Yes |
| `document.resolve_citation`       | citation id를 실제 문서 위치와 연결                 |      Yes |
| `memory.session_load`             | 세션 memory 조회                              |       No |
| `memory.session_update`           | 세션 memory 갱신                              |       No |
| `memory.approved_search`          | 검증된 장기 memory 검색                          |       No |
| `verification.citation_check`     | citation completeness 검증                  |      Yes |
| `verification.faithfulness_check` | 답변 근거성 검증                                 |      Yes |
| `dataset.candidate_create`        | 실패 사례를 평가 데이터 후보로 저장                      |       No |
| `artifact.write_event`            | interaction/context/tool/eval artifact 저장 |      Yes |

---

## 8.4 Tool Interface

```python
class Tool(Protocol):
    name: str
    version: str

    async def invoke(
        self,
        input: BaseModel,
        context: ToolExecutionContext,
    ) -> ToolResult:
        ...
```

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    interaction_id: str
    trace_id: str
    session_id: str | None
    user_id: str | None
    project_id: str | None
    app_profile: str
    agent_variant: str
    scenario_object: str | None
    scenario_depth: str | None
    permissions: list[str]
```

```python
class ToolResult(BaseModel):
    tool_name: str
    tool_version: str
    status: Literal["success", "partial", "failed"]
    output: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int
    input_hash: str
    output_hash: str | None = None
    trace_id: str
```

---

## 8.5 Tool Registry

```text
tools/
  registry.yaml
  retriever/
  document/
  memory/
  verification/
  dataset/
  artifact/
```

```yaml
tools:
  retriever.search:
    version: v1
    adapter: env:RETRIEVER_BACKEND   # opensearch | local — wired in profiles.py
    endpoint_env: OPENSEARCH_ENDPOINT
    timeout_ms: 5000
    retry: 1
    required: true

  document.resolve_citation:
    version: v1
    adapter: env:RETRIEVER_BACKEND   # opensearch | local — wired in profiles.py
    endpoint_env: OPENSEARCH_ENDPOINT
    timeout_ms: 2000
    retry: 0
    required: true

  memory.approved_search:
    version: v1
    adapter: postgres_pgvector
    timeout_ms: 1000
    retry: 0
    required: false

  verification.citation_check:
    version: v1
    adapter: local
    timeout_ms: 1000
    retry: 0
    required: true

  artifact.write_event:
    version: v1
    adapter: object_store
    timeout_ms: 1000
    retry: 1
    required: true
```

---

## 8.6 Tool Error Policy

| Error Code               | 의미                | 처리                              |
| ------------------------ | ----------------- | ------------------------------- |
| `tool_timeout`           | 제한 시간 초과          | required면 fail, optional이면 skip |
| `tool_unavailable`       | endpoint 접근 불가    | fallback 가능 여부 확인               |
| `tool_invalid_input`     | schema 검증 실패      | workflow bug로 기록                |
| `tool_empty_result`      | 결과 없음             | no result / partial 처리          |
| `tool_permission_denied` | 권한 부족             | refusal                         |
| `tool_schema_mismatch`   | output schema 불일치 | fail-fast                       |
| `tool_internal_error`    | 내부 오류             | retry 후 실패                      |

Tool 실패는 자연스러운 답변으로 덮지 않는다. 기획 문서의 에러 처리 원칙도 검색/검증 실패 시 실패를 환각으로 덮지 않고 명시적으로 처리해야 한다고 정의한다. 

---

## 9. Prompt Management Architecture

Prompt는 다음 조합으로 구성한다.

```text
Prompt = system
       + object fragment
       + depth fragment
       + cell fragment
       + context injection
       + memory injection
       + output schema
```

```text
prompts/
  registry.yaml
  system/
  object/
  depth/
  cell/
  schemas/
```

모든 실행은 다음 정보를 남긴다.

```text
prompt_profile_id
prompt_version
prompt_source
rendered_prompt_hash
model_options
```

---

## 10. Context Management Architecture

각 요청은 하나의 `ContextPack`을 생성한다.

```python
@dataclass(frozen=True)
class ContextPack:
    interaction_id: str
    query_text: str
    chat_history: list[ChatTurn]
    conversation_summary: str | None
    scenario_object: str
    scenario_depth: str
    entities: dict[str, list[str]]
    retrieved_chunk_refs: list[RetrievedChunkRef]
    citation_candidates: list[CitationCandidate]
    memory_refs: list[MemoryRef]
    tool_result_refs: list[ToolResultRef]
    context_hash: str
```

`ContextPack`은 장기 memory가 아니다.
이는 현재 답변 생성을 위한 단기 입력이며, 요청 처리 후 snapshot으로 저장된다.

---

## 11. Context Snapshot

```text
artifacts/
  context_snapshots/
    yyyy-mm-dd/
      {interaction_id}.json
```

| Mode       | 저장 내용                                       | 사용 환경   |
| ---------- | ------------------------------------------- | ------- |
| `metadata` | chunk id, document id, score, page, section | AWS 기본  |
| `snippets` | metadata + 짧은 snippet                       | AWS 실험  |
| `full`     | retrieved context full text                 | on-prem |

AWS MVP에서는 `metadata` 또는 `snippets`를 기본값으로 한다.
On-premises에서는 내부망 조건을 전제로 `full` 저장을 허용한다.

---

## 12. Memory Architecture

## 12.1 Memory 계층

| 계층                     | 목적                         | 저장소                  |
| ---------------------- | -------------------------- | -------------------- |
| Session Memory         | multi-turn context 유지      | Postgres             |
| Memory Candidate       | 장기 지식 후보                   | Postgres             |
| Expert Review          | 승인/거절/폐기                   | Postgres             |
| Approved Domain Memory | 검증된 장기 지식                  | Postgres + pgvector  |
| Evaluation Memory      | 실패 사례, golden dataset      | Git JSONL + S3/MinIO |
| Memory Archive         | rejected/deprecated memory | S3/MinIO             |

---

## 12.2 Session Memory

```python
@dataclass
class SessionMemory:
    session_id: str
    user_id: str | None
    project_id: str | None
    active_entities: dict[str, list[str]]
    active_scenario_object: str | None
    active_scenario_depth: str | None
    conversation_summary: str
    recent_turns: list[ChatTurn]
    last_retrieved_chunk_ids: list[str]
    last_memory_ids_used: list[str]
    updated_at: datetime
    expires_at: datetime | None
```

정책:

```text
- 최근 5~10턴은 직접 유지
- 이전 대화는 summary로 압축
- 후속 질문일 때만 session memory 주입
- 새 주제면 이전 session memory 억제
- 기본 TTL: 30~90일
```

기획 문서도 multi-turn context handling에서 최대 5턴 직접 유지와 요약 압축 방식을 제시한다. 

---

## 12.3 Memory Candidate

```python
@dataclass
class MemoryCandidate:
    memory_id: str
    source_interaction_id: str
    source_trace_id: str
    memory_type: str
    scenario_object: str
    scenario_depth: str
    entities: dict[str, list[str]]
    claim: str
    answer_summary: str | None
    supporting_chunk_ids: list[str]
    citations: list[str]
    verification_status: str
    expert_review_status: str
    staleness_status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
```

상태:

```text
candidate
review_required
approved
rejected
deprecated
stale
```

---

## 12.4 Approved Domain Memory

```python
@dataclass
class ApprovedMemory:
    memory_id: str
    memory_type: str
    namespace: str
    scenario_object: str
    scenario_depth: str
    entities: dict[str, list[str]]
    canonical_question: str | None
    canonical_answer: str | None
    claim: str | None
    supporting_chunk_ids: list[str]
    citations: list[str]
    source_document_revisions: list[str]
    embedding_id: str | None
    version: int
    status: str
    approved_by: str
    approved_at: datetime
    updated_at: datetime
```

---

## 12.5 Memory Injection Policy

장기 memory는 항상 prompt에 주입하지 않는다.

```text
1. 현재 질문과 scenario_object / scenario_depth가 일치하거나 강하게 연관된다.
2. memory가 approved 상태다.
3. supporting citation이 존재한다.
4. 현재 문서 revision과 충돌하지 않는다.
5. trace에 memory 사용 이력을 남길 수 있다.
6. memory가 원문 retrieved context보다 우선 근거로 사용되지 않는다.
```

---

## 13. Memory Lifecycle

```text
사용자 질문
  ↓
Runtime Context 생성
  ↓
Session Memory 조회
  ↓
Approved Memory 검색
  ↓
Tool 호출
  ↓
Agent 실행
  ↓
Context Snapshot 저장
  ↓
Interaction Event 저장
  ↓
실패 / 유의미 사례 선별
  ↓
Memory Candidate 생성
  ↓
Expert Review
  ↓
승격 여부 결정
      ├─ Approved Domain Memory
      ├─ Golden Dataset
      ├─ Rejected Archive
      └─ Deprecated Memory
```

---

## 14. Observability / Trace Architecture

### 14.1 Span Tree

```text
http.post /v1/chat/completions
└─ agent.run
   ├─ agent.intent_classification
   ├─ agent.scenario_routing
   ├─ tool.memory.session_load
   ├─ tool.retriever.search
   ├─ tool.memory.approved_search
   ├─ agent.context_build
   ├─ agent.prompt_render
   ├─ llm.generation
   ├─ tool.document.resolve_citation
   ├─ tool.verification.citation_check
   ├─ tool.verification.faithfulness_check
   ├─ memory.candidate_extract
   ├─ tool.memory.session_update
   ├─ tool.artifact.write_event
   └─ agent.response_format
```

### 14.2 공통 Span Attributes

```text
interaction_id
trace_id
agent_variant
app_profile
model_id
scenario_object
scenario_depth
classification_confidence
prompt_profile_id
prompt_version
rendered_prompt_hash
context_hash
retrieved_chunk_ids
tool_result_refs
memory_ids_used
verification_status
citation_completeness
faithfulness
latency_ms
```

### 14.3 Tool Span Attributes

```text
tool.name
tool.version
tool.adapter
tool.status
tool.input_hash
tool.output_hash
tool.latency_ms
tool.retry_count
tool.error_code
tool.required
tool.cache_hit
tool.permission_scope
```

---

## 15. Event / Artifact Store

```text
artifacts/
  interaction_events/
    yyyy-mm-dd/
      *.jsonl

  context_snapshots/
    yyyy-mm-dd/
      {interaction_id}.json

  prompt_render_records/
    yyyy-mm-dd/
      {interaction_id}.json

  tool_result_records/
    yyyy-mm-dd/
      {interaction_id}.json

  memory_artifacts/
    candidates/
    approved/
    deprecated/

  eval_runs/
    yyyy-mm-dd/
      {eval_run_id}/
        config.json
        dataset.jsonl
        results.jsonl
        scores.csv
        failures.jsonl
        prompt_versions.json
        memory_versions.json
```

---

## 16. InteractionEvent Schema

```python
@dataclass(frozen=True)
class InteractionEvent:
    schema_version: str
    interaction_id: str
    trace_id: str
    timestamp: str

    app_profile: str
    agent_variant: str
    model_id: str

    query_text_hash: str
    query_text_sample: str | None

    scenario_object: str
    scenario_depth: str
    classification_confidence: float

    prompt_profile_id: str
    prompt_version: str
    rendered_prompt_hash: str

    context_hash: str
    retrieval_doc_count: int
    retrieved_chunk_ids: list[str]
    retrieval_confidence: float

    tool_calls: list[dict]

    memory_ids_used: list[str]
    memory_types_used: list[str]
    memory_retrieval_scores: dict[str, float]
    memory_review_statuses: dict[str, str]
    memory_staleness_statuses: dict[str, str]

    answer_hash: str
    citation_ids: list[str]

    verification_status: str
    citation_completeness: float
    faithfulness: float

    latency_ms: int
    token_usage: dict[str, int]

    refusal_reason: str | None
    error_code: str | None
```

---

## 17. State DB Schema

```sql
CREATE TABLE session_memory (
    session_id TEXT PRIMARY KEY,
    user_id TEXT,
    project_id TEXT,
    active_entities JSONB NOT NULL DEFAULT '{}',
    active_scenario_object TEXT,
    active_scenario_depth TEXT,
    conversation_summary TEXT,
    recent_turns JSONB NOT NULL DEFAULT '[]',
    last_retrieved_chunk_ids JSONB NOT NULL DEFAULT '[]',
    last_memory_ids_used JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE TABLE memory_candidates (
    memory_id TEXT PRIMARY KEY,
    source_interaction_id TEXT NOT NULL,
    source_trace_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scenario_object TEXT,
    scenario_depth TEXT,
    entities JSONB NOT NULL DEFAULT '{}',
    claim TEXT,
    answer_summary TEXT,
    supporting_chunk_ids JSONB NOT NULL DEFAULT '[]',
    citations JSONB NOT NULL DEFAULT '[]',
    verification_status TEXT,
    expert_review_status TEXT NOT NULL DEFAULT 'candidate',
    staleness_status TEXT NOT NULL DEFAULT 'unknown',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE approved_memories (
    memory_id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    namespace TEXT NOT NULL,
    scenario_object TEXT,
    scenario_depth TEXT,
    entities JSONB NOT NULL DEFAULT '{}',
    canonical_question TEXT,
    canonical_answer TEXT,
    claim TEXT,
    supporting_chunk_ids JSONB NOT NULL DEFAULT '[]',
    citations JSONB NOT NULL DEFAULT '[]',
    source_document_revisions JSONB NOT NULL DEFAULT '[]',
    embedding vector,
    version INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'approved',
    approved_by TEXT NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tool_call_records (
    tool_call_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    error_code TEXT,
    latency_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE expert_reviews (
    review_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dataset_candidates (
    dataset_candidate_id TEXT PRIMARY KEY,
    source_interaction_id TEXT NOT NULL,
    source_trace_id TEXT NOT NULL,
    failure_type TEXT,
    scenario_object TEXT,
    scenario_depth TEXT,
    question TEXT NOT NULL,
    expected_answer TEXT,
    expected_citations JSONB NOT NULL DEFAULT '[]',
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 18. Evaluation Architecture

### 18.1 평가 레벨

| 레벨                 | 목적                                    | 도구                           |
| ------------------ | ------------------------------------- | ---------------------------- |
| Contract Test      | schema, citation, refusal, routing 검증 | pytest                       |
| Smoke Eval         | 주요 cell별 최소 질문 검증                     | pytest + Ragas               |
| Prompt Matrix Eval | prompt version 비교                     | Promptfoo                    |
| Tool Eval          | tool failure, timeout, schema 검증      | pytest                       |
| Memory Eval        | session, approved memory, stale 차단 검증 | custom eval                  |
| Full Eval          | golden dataset 기반 회귀 평가               | eval-runner + Phoenix        |
| Expert Review      | 도메인 전문가 판단                            | Phoenix dataset / CSV export |

---

## 18.2 주요 Metric

| Metric                         | 의미                        |
| ------------------------------ | ------------------------- |
| `intent_accuracy`              | O/D 분류 정확도                |
| `retrieval_hit_at_k`           | 기대 문서가 top-k에 포함되는지       |
| `context_precision`            | 관련 chunk가 상위에 배치되는지       |
| `citation_resolvability`       | citation id가 실제 문서에 연결되는지 |
| `citation_completeness`        | 주요 claim마다 citation이 있는지  |
| `faithfulness`                 | 답변 claim이 context로 뒷받침되는지 |
| `tool_success_rate`            | tool 호출 성공률               |
| `tool_latency_p95`             | tool latency              |
| `unapproved_memory_usage_rate` | 승인되지 않은 memory 사용률        |
| `stale_memory_usage_rate`      | stale memory 사용률          |
| `session_followup_accuracy`    | 후속 질문 처리 정확도              |
| `topic_shift_suppression`      | 새 주제에서 이전 memory 억제율      |

---

## 18.3 MVP Gate

```text
citation_resolvability = 100%
unsupported_answer_rate = 0 severe case
verification_pass_rate >= 70%
faithfulness_avg >= 0.80
context_precision_avg >= 0.70

unapproved_memory_usage_rate = 0
stale_memory_usage_rate = 0 severe case
memory_citation_validity = 100%
topic_shift_suppression >= 0.90
session_followup_accuracy >= 0.80

required_tool_success_rate >= 0.95
tool_schema_mismatch = 0
```

---

## 19. Deployment Architecture

### 19.1 Local

```text
local
├─ agent-api
├─ postgres + pgvector
├─ phoenix
├─ otel-collector
├─ grafana
├─ tempo
├─ prometheus
├─ loki
├─ minio
└─ eval-runner
```

---

### 19.2 AWS MVP

```text
AWS EC2
├─ agent-api
├─ postgres + pgvector
├─ phoenix
├─ otel-collector
├─ grafana
├─ tempo
├─ prometheus
├─ loki
└─ eval-runner

AWS S3
└─ artifacts
```

AWS MVP에서는 운영용 managed architecture보다 **온프레미스 이전 가능성**이 중요하다.

---

### 19.3 On-Premises

```text
On-prem VM / bare metal
├─ agent-api
├─ postgres + pgvector
├─ phoenix
├─ otel-collector
├─ grafana
├─ tempo
├─ prometheus
├─ loki
├─ minio
└─ eval-runner
```

조건:

```text
- 외부 network 없이 실행 가능
- Docker image 사전 반입 가능
- prompt bundle local mount 가능
- tool registry local mount 가능
- artifact store는 MinIO 사용
- model endpoint와 retriever endpoint는 env로 주입
- full context snapshot은 on-prem에서만 허용
```

---

## 20. Docker Compose 추가 서비스

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: agent_state
      POSTGRES_USER: agent
      POSTGRES_PASSWORD: agent
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ../postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    profiles: ["local", "aws-mvp", "onprem"]

  qdrant:
    image: qdrant/qdrant:latest
    profiles: ["memory-scale"]
    ports:
      - "6333:6333"
    volumes:
      - qdrant_data:/qdrant/storage

volumes:
  postgres_data:
  qdrant_data:
```

MVP 기본은 `Postgres + pgvector`다.
Qdrant는 approved memory 규모가 커졌을 때 `memory-scale` profile로 활성화한다.

---

## 21. Environment Variables

```bash
APP_PROFILE=local
AGENT_VARIANT=sequential_verified_rag_v1
EXPOSED_MODEL_ID=agent-search-v1

LLM_ENDPOINT=https://llm-api.example.com
RETRIEVER_BACKEND=opensearch              # opensearch | local (fake)
OPENSEARCH_ENDPOINT=http://opensearch:9200
OPENSEARCH_INDEX=smr-docs

OTEL_ENABLED=true
OTEL_SERVICE_NAME=smr-agent-backend
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

PHOENIX_ENABLED=true
PHOENIX_ENDPOINT=http://phoenix:6006

PROMPT_SOURCE=hybrid
PROMPT_LOCAL_DIR=/app/prompts

TOOL_REGISTRY_PATH=/app/tools/registry.yaml
TOOL_TRACE_ENABLED=true
TOOL_TIMEOUT_DEFAULT_MS=3000
TOOL_RETRY_DEFAULT=0

STATE_DB_URL=postgresql+psycopg://agent:agent@postgres:5432/agent_state

MEMORY_ENABLED=true
MEMORY_STORE=postgres
MEMORY_VECTOR_BACKEND=pgvector
MEMORY_REVIEW_REQUIRED=true
MEMORY_APPROVED_ONLY=true
MEMORY_SESSION_TTL_DAYS=90
MEMORY_STALENESS_CHECK_ENABLED=true

EVENT_SINK=minio
EVENT_BUCKET=smr-agent-events
EVENT_PREFIX=mvp

CONTEXT_CAPTURE_MODE=metadata
TRACE_CONTENT_MODE=metadata
```

On-prem:

```bash
APP_PROFILE=onprem
EXTERNAL_TOOL_CALLS_ENABLED=false

EVENT_SINK=minio
MINIO_ENDPOINT=http://minio:9000

PROMPT_SOURCE=local
CONTEXT_CAPTURE_MODE=full
TRACE_CONTENT_MODE=full

RETRIEVER_BACKEND=opensearch
OPENSEARCH_ENDPOINT=http://internal-opensearch:9200
OPENSEARCH_INDEX=smr-docs
```

---

## 22. Repository 구조

```text
repo/
  backend/
    app/
      api/
      application/
        agents/
        routing/
        tools/
          registry.py
          executor.py
          policy.py
          errors.py
        memory/
          resolver.py
          store.py
          review_service.py
          policies.py
      domain/
        context/
        memory/
        tools/
      ports/
        tool.py
        memory_store.py
        artifact_store.py
        vector_store.py
      adapters/
        tools/
          retriever_http.py
          document_http.py
          verification_local.py
        postgres/
        vector/
        artifact/
      observability/
      prompting/
      context/
      evaluation_hooks/
    Dockerfile

  prompts/
    registry.yaml
    system/
    object/
    depth/
    cell/
    schemas/

  tools/
    registry.yaml
    retriever/
    document/
    memory/
    verification/
    dataset/
    artifact/

  datasets/
    smoke/
    golden/
    memory/
    tool/

  evals/
    runner/
      metrics/
        citation.py
        faithfulness.py
        scenario.py
        refusal.py
        memory_relevance.py
        memory_staleness.py
        tool_success.py

  infra/
    compose/
    env/
    postgres/
      init.sql
      migrations/
    qdrant/
    otel/
    grafana/
    minio/

  scripts/
    run-local.sh
    run-aws-mvp.sh
    run-onprem.sh
    run-eval-smoke.sh
    run-eval-memory.sh
    run-eval-tool.sh
```

---

## 23. 구현 단계

| Phase     | 목표                               |
| --------- | -------------------------------- |
| Phase 0   | Docker 기반 환경 골격                  |
| Phase 1   | Agent 실행 trace                   |
| Phase 2   | Prompt / Context 관리              |
| Phase 2.5 | Tool Registry / Tool Executor    |
| Phase 3   | Session Memory                   |
| Phase 3.5 | Verification Tool Integration    |
| Phase 4   | Memory Candidate / Expert Review |
| Phase 5   | Approved Domain Memory Search    |
| Phase 6   | Evaluation Loop                  |
| Phase 7   | AWS MVP 배포                       |
| Phase 8   | On-Premises Profile              |

---

## 24. Dashboard

### Runtime Dashboard

```text
request_count
request_latency_p95
error_rate
timeout_rate
token_usage_total
```

### Tool Dashboard

```text
tool_call_count
tool_success_rate
tool_latency_p95
tool_error_rate
tool_timeout_count
retriever_empty_result_rate
citation_resolve_fail_rate
memory_approved_search_hit_rate
```

### Memory Dashboard

```text
session_memory_count
active_session_count
memory_candidate_count
approved_memory_count
rejected_memory_count
stale_memory_count
memory_review_pending_count
memory_injection_rate
```

### Evaluation Dashboard

```text
latest_smoke_eval_score
latest_full_eval_score
memory_eval_score
tool_eval_score
regression_count
failed_golden_questions
```

---

## 25. 검수 기준

```text
1. local / aws-mvp / onprem profile이 같은 image를 사용한다.
2. AGENT_VARIANT 변경으로 workflow variant를 교체할 수 있다.
3. 모든 Agent 실행은 interaction_id와 trace_id를 가진다.
4. node-level trace가 Phoenix 또는 Tempo에서 확인된다.
5. Tool Registry가 존재하고 tool name/version/schema/timeout/retry가 정의되어 있다.
6. Tool 호출은 ToolExecutor를 통해 수행된다.
7. 모든 Tool 호출은 trace span으로 기록된다.
8. Required Tool 실패 시 Agent가 근거 없는 답변을 생성하지 않는다.
9. Optional Tool 실패 시 Agent는 fallback하고 실패 기록을 남긴다.
10. prompt_profile_id, prompt_version, rendered_prompt_hash가 기록된다.
11. context_hash와 retrieved_chunk_ids가 기록된다.
12. session memory가 Postgres에 저장되고 TTL 정책을 가진다.
13. Memory Candidate는 승인 전 답변에 주입되지 않는다.
14. Approved Memory만 MemoryResolver를 통해 검색된다.
15. stale memory는 답변에 주입되지 않는다.
16. memory_ids_used가 trace와 InteractionEvent에 기록된다.
17. eval-runner가 smoke / memory / tool dataset을 실행한다.
18. AWS에서는 S3, On-prem에서는 MinIO로 artifact store를 교체할 수 있다.
19. On-prem profile에서는 외부 network tool 호출을 비활성화할 수 있다.
20. 모델 서빙과 OpenSearch 상세 구성 없이도 Agent 실험 인프라가 실행된다.
```

---

## 26. 최종 시스템 정의

이 MVP 시스템은 다음을 위한 플랫폼이다.

```text
Agent Backend를 실행하고,
Agent workflow를 variant로 실험하고,
Tool 호출을 schema와 trace 기반으로 통제하고,
Prompt와 Context를 재현 가능하게 기록하고,
Session Memory로 multi-turn context를 관리하고,
Memory Candidate를 expert review를 통해 검증하며,
Approved Domain Memory만 장기 지식으로 재사용하고,
평가 Dataset으로 회귀 테스트하며,
AWS 실험 환경과 온프레미스 환경을 같은 Docker 구조로 유지하는 시스템.
```

본 아키텍처의 중심 구성은 다음이다.

```text
AgentRunner
ToolRegistry
ToolExecutor
ToolExecutionContext
ToolResult

PromptResolver
ContextPack
MemoryResolver
MemoryStore
MemoryReviewService

VerificationPolicy
OpenTelemetry Trace
Phoenix Experiment
Postgres + pgvector
S3 / MinIO Artifact Store
Eval Runner
```

---

# 최종 요약

이 설계의 핵심은 다음이다.

```text
Agent는 단순 LLM 호출이 아니다.
Agent는 Workflow, Tool, Context, Memory, Evaluation이 결합된 실행 시스템이다.
```

그리고 MVP에서 가장 중요한 원칙은 다음이다.

```text
Tool은 통제 가능해야 한다.
Context는 재현 가능해야 한다.
Memory는 검증 후 승격되어야 한다.
Evaluation은 개발 루프의 일부여야 한다.
Deployment는 AWS와 On-premises를 동시에 고려해야 한다.
```

[1]: https://docs.docker.com/compose/?utm_source=chatgpt.com "Docker Compose"
[2]: https://opentelemetry.io/docs/collector/ "Collector | OpenTelemetry"
[3]: https://arize.com/docs/phoenix/self-hosting "Self-Hosting - Phoenix"
[4]: https://grafana.com/docs/tempo/latest/set-up-for-tracing/setup-tempo/deploy/locally/docker-compose/?utm_source=chatgpt.com "Deploy Tempo using Docker Compose"
[5]: https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/ "Faithfulness - Ragas"
[6]: https://www.promptfoo.dev/docs/intro/ "Intro | Promptfoo"