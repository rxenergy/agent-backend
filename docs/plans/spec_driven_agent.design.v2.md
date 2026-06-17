# spec_driven_v2 — 2-노드(DGX Spark) 분산 검색 Agent 설계

## 배경

두 DGX Spark 노드에 각각 vLLM(gemma-4-awq)이 서빙된다.
- **Node1** `http://192.168.100.10:8001/v1` — LLM pool id `gemma-4-26b`
- **Node2** `http://192.168.100.11:8001/v1` — LLM pool id `gemma-4-26b-sub`

`spec_driven_v1` 은 모든 LLM 노드(N1 spec·N2 쿼리·N3.5 외부참조·N4 생성)를 Node1 한 곳에서
돈다. `spec_driven_v2` 는 LLM 업무를 두 노드로 분할해, **외부 참조 문서 선별(follow_up)을
Node2 로 떼어내고** 슬롯 단위 검증(verify_slot)은 Node1 에서 돈다. 검증 결과로 N4 컨텍스트를
정제한다.

## 노드 분할

| 노드 | LLM(pool id) | 담당 |
|------|--------------|------|
| Node1 | `gemma-4-26b`(요청 resolved = utility_llm) | N1 Define Spec/slot, N2 Query Formulation, **슬롯 단위 1차 검색 결과 검증**(`retrieval.verify_slot`), N4 Generation |
| Node2 | `gemma-4-26b-sub`(`SECONDARY_LLM`) | **외부 참조 문서 선별**(enhanced `retrieval.follow_up`) |

노드는 **pool id 로 핀**한다(IP/엔드포인트는 `LLM_POOL` env 가 소유). 검증/외부참조 도구 모두
생성 시점에 LLM 이 주입돼 요청 `model` 을 추종하지 않는다(재현성 — 고정 정책 입력).

## 흐름

```
N0 Triage(Node1) → N1 Define Spec(Node1) → N2 Query Formulation(Node1)
  → N3 1차 검색(retrieval.search, LLM 아님)
  → [per-slot 파이프라인 — 배리어 없이 동시]
       Stage1 Node1 verify(retrieval.verify_slot)
         입력: 사용자 질문 + answer_spec + 슬롯 쿼리 + 1차 검색 결과
         출력: 필요 청크 식별자 + 멀티홉 필요 청크 식별자
       Stage2 Node2 외부참조 선별(retrieval.follow_up, necessity_only)
         입력: 사용자 질문 + answer_spec + 슬롯 쿼리 + 멀티홉 청크
         출력: 답변에 꼭 필요한 외부 참조 + 재검색 쿼리
       Stage3 Node2 2차 검색(retrieval.search, 참조 문서 내부)
  → N4 Generation(Node1)
```

**병렬성**: 각 슬롯이 독립 task 로 돌아 배리어가 없다. slot 1 이 Stage2(Node2)로 넘어갈 때
slot 2 는 Stage1(Node1 검증)을 돈다 — 두 노드가 별개 vLLM 이라 실제로 겹친다. 동시 슬롯 수는
`SPEC_DRIVEN_V2_VERIFY_CONCURRENCY`(러너 semaphore) + verify 도구 내부 semaphore 로 캡한다
(공유 vLLM KV-cache 안전판).

**결정성**: `asyncio.gather` 로 동시 실행하되, 완료 순서와 무관하게 슬롯 *원순서*로 순차
병합한다(모든 변이 — record/dedup/카운터 — 는 gather 후 순차 루프에서만; v1 idiom 보존).

## N4 컨텍스트 (v1 과의 차이)

v1 은 "1차 검색 결과 전량 보존 + 2차 score 순 채움". **v2 는 1차 전량 보존을 폐기**하고
**Node1 이 고른 필요 청크(necessary) ∪ 2차(멀티홉) 결과만** 쓴다. 토큰 예산 거버너
(`_assemble_final_chunks`)는 necessary 를 always-include 자리에 넣어 재사용한다.

## 고도화된 `retrieval.follow_up`

기존 도구를 확장한다(별도 도구 신설 X, 하위호환): 입력에 `answer_spec`·`slot_query`·
`necessity_only` 옵셔널 필드 추가. `necessity_only=True` 면 청크의 *모든* 외부 참조가 아니라
answer_spec+slot_query 기준 "답변에 꼭 필요한" 참조만 선별한다(`SYSTEM_PROMPT_NECESSITY`).
v1 은 새 필드를 안 넘기므로 byte-identical.

## Degrade / 운영

- **verify 미가용/미배선**(Node1 검증) → verify 도구 graceful skip 또는 슬롯 fallback
  (method="fallback", necessary=전량, 멀티홉 없음) → 단일노드(v1식 전량 보존)로 동작.
- **Node2 미가용/미배선**(외부참조 선별) → follow_up graceful skip → 2차 검색 없음(1차 검증
  결과만으로 N4). Node2 복구는 `make up-onprem-sub`.
- **SECONDARY_LLM 빈 값** → `default_llm` 폴백(단일노드). **pool 에 없는 값** → boot fail-fast.
- follow_up 도구는 SECONDARY_LLM pool 엔트리가, verify 도구는 utility_llm pool 엔트리가
  각각 `openai_compat`(내부망 vLLM)일 때만 배선(guided_json 요구 — anthropic/fake 비호환).

## 구현 위치

- 러너: `backend/app/application/agents/spec_driven_v2.py`(`SpecDrivenV2Runner`,
  `_post_retrieval` 시임 오버라이드 + `_run_slot_pipeline`). 시임 계약은 v1
  `_PostRetrievalOutcome`.
- Node1 검증: `adapters/tools/retrieval_verify_slot.py` + `adapters/slot_verifier_llm.py`
  + `ports/slot_verifier.py` + 도메인 `VerifySlotInput`/`VerifySlotResult`.
- 프롬프트: `prompts/spec_driven/verify_slot_v2.md` + `schemas/verify_slot_v2.json`
  (registry `spec_driven_verify_prompts.spec_driven_verify_v2`, sha 핀).
- 배선: `config/profiles.py`(follow_up→SECONDARY_LLM / verify→utility_llm + v2 프롬프트 source),
  `config/settings.py`(`secondary_llm`, `spec_driven_v2_verify_*`),
  `tools/registry.yaml`(`retrieval.verify_slot`, `retrieval.follow_up` v2),
  `variants/registry.yaml`(`spec_driven_v2`).

## 재현 핀(`query_understanding.spec_driven`)

- `node1_llm_id` — 생성/쿼리/검증을 돈 LLM.
- `verify` — `{node1, num_slots, total_necessary, total_multihop, added_second_pass,
  slots:[{slot, method, num_first_pass, num_necessary, num_multihop}]}`.
- `retrieval.necessary_kept` / `first_pass_total` — 1차 전량이 아니라 necessary 기준임을 가시화.
- `follow_up.necessity_only=true` — 필요-판정 선별 모드.
- `context_budget.necessary_dropped` — 윈도우 안전판이 necessary 를 밀어낸 비정상 신호.

## OTel

`agent.slot_verify` phase span(KIND_CHAIN)이 per-slot 파이프라인을 묶고, 그 아래
`tool.vllm_verify.retrieval.verify_slot`(Node1)·`retrieval.follow_up`(Node2)·
`retrieval.search` tool span 이 슬롯별로 nesting 된다(task 생성 시점 컨텍스트 캡처). 도구
이름으로 노드별 호출이 메트릭에서 구분된다.
