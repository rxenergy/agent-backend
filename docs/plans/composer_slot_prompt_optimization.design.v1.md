# composer 슬롯 프롬프트 최적화 — generation_v1 수준 정보 심도 회복 설계 v1

`composer` variant 의 N4 슬롯 파이프라인이 단일-경로 `generation_v1.md` 와 **동등한 정보 심도**
(조문 다층 전개·정량값+기술적 근거·주장↔판단 대조·SER/RAI 조건 verbatim 보존)로 각 구획을
출력하도록 슬롯/종합 프롬프트와 그 입력 컨텍스트를 최적화하는 계획.

대상:
- `prompts/spec_driven/composer_slot_v1.md` (N4.1 슬롯 1구획 생성)
- `prompts/spec_driven/composer_synthesize_v1.md` (N4.3 닫음 블록)
- `backend/app/application/agents/composer.py`
  `_render_slot_prompt`(866) / `_render_context_subset`(905) / `_plan_slots`(826) /
  `_slot_model_options`(1129) / `_render_synthesize_prompt`(959)
- `prompts/registry.yaml` `composer_slot_prompts` 블록(177)

기준선: `prompts/spec_driven/generation_v1.md` (단일-경로 N4, 4축 전개 규약 완비).

---

## 0. 배경 — 두 경로가 같은 근거를 다르게 조직한다

composer 는 `SpecDrivenRunner` 를 상속해 N0~N3.5(분류·사양·쿼리·검색·후속·세션)를 *그대로*
계승하고 **N4 Generation 만** 슬롯 단위로 대체한다(`composer.py:56` docstring). 즉:

- **입력(CONTEXT)은 동일** — 같은 검색·조립·토큰예산을 거친 같은 `ContextPack`.
- **출력 조직만 다름** — generation_v1 은 *전체 CONTEXT 1회 → 답변 전체 1콜*; composer 는
  *슬롯별 CONTEXT 서브셋 → 슬롯당 1콜*(`_generate_slotwise`, `composer.py:462`).

슬롯 분할 자체는 지연·집중도 면에서 의도된 설계다. 문제는 **슬롯 프롬프트가 generation_v1 의
4축 전개 규약을 facet 한 축으로만 압축**해, 같은 근거를 받고도 더 얕게 쓴다는 점이다. 본 계획은
슬롯 경로가 generation_v1 의 심도를 *구획 단위로* 회복하게 만든다.

---

## 1. 진단 — 슬롯 경로가 generation_v1 보다 얕아지는 지점

### 1.1 프롬프트 측 (composer_slot_v1.md)

| # | 결함 | generation_v1 대비 | 효과 |
|---|---|---|---|
| D1 | **4축이 facet 1축으로 축소** — 슬롯은 "자기 facet 축만 전개, 다른 축은 다른 섹션 몫"(`composer_slot_v1.md:24`). 그러나 generation_v1 은 *한 단계 안에서도* 여러 축을 겹쳐 전개(Axis1 조문층 + Axis3 값+근거를 같은 단락에) | generation_v1 §"Compose to expert depth" 4축 동시 | 한 구획이 1차원으로 평탄 — 예: `requirement` 슬롯이 조문층은 펼치나 그 안의 정량 기준값(Axis3)은 "다른 섹션 몫"이라 누락 |
| D2 | **technical_basis 6요소·requirement 5층의 *전개 강도* 지시가 약함** — facet→축 매핑은 한 줄 bullet(`:27`~`:35`)로 압축돼, generation_v1 의 layer-by-layer 강제(§Axis1 1~5, §Axis3 1~6)와 self-check(§"depth self-check")가 없음 | generation_v1 §Axis1~4 + §"depth self-check" 5줄 | 6요소 중 1~2개만 전개하고 멈춤(요약형) |
| D3 | **CORPUS CONTEXT(scope 설명) 부재** — 슬롯 프롬프트엔 US600/US460·10CFR 볼륨·status/design 축 설명이 없음. generation_v1 §"CORPUS CONTEXT"가 *근거의 기반(edition/design)을 명시*하라 함 | generation_v1 §"CORPUS CONTEXT"(L3~31) | 슬롯이 어느 design/edition 근거인지 명시 못 함 — 전문가가 가장 필요로 하는 register 누락 |
| D4 | **authority 사다리 어법 규약 부재** — binding/guidance/review/applicant 어법 보정(requires/one acceptable method/states/was judged)이 슬롯 프롬프트에 없음. facet bullet 안에 단편적으로만("guidance wording", "review wording") | generation_v1 §"Authority hierarchy"(L40~48) | 권위 인플레(guidance 를 의무처럼)·register 혼선 |
| D5 | **SER/RAI verbatim 보존 강제가 facet bullet 한 줄** — `review_finding`/`open_item_condition` 슬롯에서 generation_v1 의 "Preserve SER conditions, ITAAC, COL items verbatim · RAI issue+resolution 분리 · contestedness signal"(§Axis2 "Mine the SER/RAI record")가 압축됨 | generation_v1 §Axis2 SER/RAI 블록(L83~86) | 조건/ITAAC 누락, RAI 쟁점 압축 |

### 1.2 컨텍스트 구성 측 (composer.py) — **관측 증상의 직접 원인**

관측된 증상: **(1) "근거 부족"이 잦다 · (2) 인용되는 청크 개수가 작다 · (3) 답변이 슬롯
단위로 인위적으로 쪼개져 수준이 낮아 보인다.** 그 메커니즘:

**슬롯 CONTEXT 가 *자기 쿼리가 회수한 청크*로만 좁혀진다.** 검색 단(`composer.py:261`)에서
`slots_by_chunk` 는 청크를 **그 청크를 회수한 쿼리의 `q.slot_name`** 으로만 귀속한다. 이어
`_plan_slots`(838)는 슬롯 CONTEXT 를 *그 슬롯에 귀속된 청크*로만 추리고 `slot_context_k`
(기본 6, `:104`)로 자른다. 결과:

```
전체 chunks: 20개(풍부)  →  슬롯 A 가 보는 CONTEXT: A 쿼리가 건진 ≤6개뿐
                            (B·C 쿼리가 건진 같은 조문의 정량값·표·SER 조건은 못 봄)
```

→ 슬롯이 전개에 필요한 근거 조각을 *전체 pack 에는 있는데도* 못 봐서 **"근거 부족" 표기 多 ·
슬롯당 인용 cite 수↓**(증상 1·2). 그리고 슬롯마다 *서로 다른 좁은 근거*로 따로 쓰니 구획이
서로 안 맞물려 **인위적 분할**처럼 읽힌다(증상 3).

| # | 결함 | 위치 | 효과 |
|---|---|---|---|
| C1 | **슬롯 CONTEXT 가 귀속 청크로만 좁혀짐(위 메커니즘)** — 전체 chunks 는 풍부한데 슬롯은 자기 쿼리 회수분 ≤`slot_context_k` 만 봄 | `_plan_slots`(838·842·845) | **증상 1·2·3 의 직접 원인** — 근거 굶음·인용↓·분할감 |
| C2 | **PRIOR SECTIONS 가 *요지 digest* 만** — `digest_lines`(`composer.py:579`)는 슬롯명+첫 문장+사용 cite-ID 1줄. 이전 구획 *전문*이 아니라 한 줄 요지라 다음 슬롯이 앞 내용에 자연스럽게 *이어쓸* 맥락이 부족 | `_render_slot_prompt`(875~882), `composer.py:579` | 구획 간 연결이 끊겨 **분할감 가중**(증상 3) |
| C3 | **`# ANSWER SPEC` 블록 미전달** — 단일-경로는 `_render_spec_block`(spec 전체: intent·answer_structure·all slots·facet)을 `# ANSWER SPEC` 으로 싣는다(`spec_driven_v1.py:1111`). 슬롯 프롬프트는 `# THIS SECTION` 에 *이 슬롯 1개*만 전달 — 전역 answer_structure 의 단계별 sub-facet 심도 지시를 슬롯이 못 봄 | `_render_slot_prompt`(888) | answer_structure 단계 심도 지시가 슬롯에 도달 안 함 |
| C4 | **슬롯 max_tokens=3000 이 다층 전개+전문 PRIOR 에 빠듯** — 단일-경로는 16384(`registry.yaml:166`). 슬롯 CONTEXT 확대 + 이전 섹션 전문 동봉이면 입출력 모두 늘어 3000 에서 절단 위험 | `registry.yaml:182`, `_slot_model_options`(1134) | 깊게 쓰기 시작한 구획이 토큰 한계로 잘림 |
| C5 | **표(`# TABLES`) facet 별 표 인용 규약 부재** — `_render_context_subset`(935~950)는 서브셋 표를 `# TABLES` 로 싣지만, 슬롯 프롬프트엔 "표의 [cite-N] 을 본문 chunk 와 구분해 인용"(generation_v1 §"Source tables") 규약이 없음 | `composer_slot_v1.md` 출력 규약 | 표 출처 cite 오귀속 |

**근본 원인 (둘):**
1. **컨텍스트 굶음(C1·C2)** — 슬롯을 *그 슬롯 쿼리가 건진 청크*로 격리한 설계가, 전체 pack 이
   풍부해도 각 구획을 근거 빈곤 상태로 만든다. 이것이 사용자가 보고한 "근거 부족·인용↓·
   인위적 분할"의 직접 원인이다.
2. **표현 분업 과잉(D1·P4)** — 슬롯 프롬프트의 "facet 1축만, 나머진 다른 섹션 몫" 가정이
   각 구획을 1차원으로 깎는다. 분업은 *중복 회피* 용도여야지 *심도 삭감* 용도가 아니다.

§3.3·§3.6 이 ①을, §3.1 이 ②를 해소한다.

---

## 2. 설계 원칙

| # | 원칙 | 근거 |
|---|---|---|
| P1 | **groundedness 불변.** 깊은 전개도 *이 슬롯 CONTEXT* 근거로만. 서브셋 밖 cite 는 L0 게이트가 제거(`_verify_slot`, `composer.py:985`). | CLAUDE.md #6 |
| P2 | **표현=모델 / 조립=결정론.** 어떤 깊이로 전개할지는 프롬프트(모델). 슬롯 순서·CONTEXT 서브셋·헤더는 결정론(`_plan_slots`). | `feedback_model_over_rule` |
| P3 | **generation_v1 을 single source of depth 로.** 슬롯 프롬프트는 generation_v1 의 4축·authority·CORPUS·self-check 를 *슬롯 facet 범위로 투영*하되 **축을 버리지 않는다** — facet 은 "주축"이지 "유일 축"이 아니다. | 본 진단 D1 |
| P4 | **분업은 중복 회피 용도.** PRIOR SECTIONS 는 "이미 쓴 걸 반복 말라"는 신호이지, "내 축 외엔 쓰지 말라"가 아니다. 각 구획은 자기 facet 을 주축으로 *그에 딸린 값·조건·권위*까지 풀로 전개. | composer_slot_v1.md §"Build on prior" 재해석 |
| P5 | **net-neutral 길이.** generation_v1 규약을 슬롯에 가져오되, "전체 답 구조/서론·결론 금지"처럼 슬롯에 무의미한 규약은 빼서 길이 상쇄. | 기존 regression 관행 |
| P6 | **새 variant 금지·기존 불변.** composer.py 와 슬롯 프롬프트만 수정. spec_driven_v1.py·generation_v1.md 불변. 프롬프트 sha256 갱신·registry 동기화. | `feedback_no_legacy`, CLAUDE.md |
| P7 | **재현성.** 프롬프트 변경 = 새 rendered_prompt_hash. sha256 갱신, slot_pins 의 rendered_prompt_hash 자동 반영. | CLAUDE.md #5 |

---

## 3. 변경 설계

### 3.1 composer_slot_v1.md 재작성 — generation_v1 의 심도 규약을 슬롯 facet 범위로 투영

슬롯 프롬프트를 다음 골격으로 재작성(generation_v1 의 해당 절을 슬롯 1구획 범위로 좁혀 이식):

1. **역할 1줄 + grounding rule** (현행 유지 — `:1`~`:12`).
2. **CORPUS CONTEXT(축약)** (신규, D3 해소) — generation_v1 §"CORPUS CONTEXT" 의 핵심만:
   status(RG/SRP/DSRS) vs design(US600/US460/PreApp) 축, 10CFR 볼륨, "scope 기반을 한 줄로
   명시하되 새 규제 주장 금지". 슬롯이 *자기 근거의 edition/design 기반*을 밝히게 함.
3. **Authority hierarchy(축약)** (신규, D4 해소) — binding/guidance/review_record/applicant_claim
   4등급 어법 보정 표 1개. `# THIS SECTION` 의 `expected_authority`·`governing_normative_class`
   와 연결.
4. **이 facet 의 주축 전개 — 축을 버리지 말 것** (D1·D2 해소, 핵심 변경):
   - 현행 facet bullet(`:27`~`:35`)을 유지하되, **"주축(facet) + 그에 딸린 부축"** 으로 강화:
     - `requirement` 주축 → 조문 5층(상위근거→operative wording→component items→applicability
       →sub-rules) 전개 + **그 요건이 *수치로* 고정되면 그 값+단위+조건도 같이**(Axis3 투영).
     - `technical_basis`/`quantitative_limit` 주축 → 값 6요소(origin→companion→method→
       conservatism/margin→applicability→revision) 풀 전개 + **그 값의 근거 조문 어법**(Axis까지).
     - `review_finding`/`open_item_condition` 주축 → SER 조건·ITAAC·COL **verbatim 보존**,
       RAI 쟁점+해소 분리, contestedness signal — generation_v1 §Axis2 SER/RAI 블록 이식.
     - `applicant_design` ↔ `review_finding` 는 *별 구획*이지만, 한 구획 안에서도 주장과
       판단이 같이 있으면 **분리·귀속**(claim vs finding) — register 보존.
   - "다른 섹션이 다룰 *주 facet* 은 겹쳐쓰지 말되, **내 구획을 입증하는 데 필요한 부축은
     생략하지 말 것**" 으로 분업 규약 재서술(P4).
5. **표 인용 규약(축약)** (신규, C5 해소) — generation_v1 §"Source tables": `# TABLES` 의 표는
   자기 [cite-N] 으로, 본문 narrative 는 본문 chunk 의 [cite-N] 으로.
6. **출력 형식(현행 유지)** — 헤더 금지(assembler 가 붙임)·서론/결론 금지·markdown 표/목록·
   inline 한계(`근거 부족`) (`:40`~`:45`).
7. **구획 self-check(신규, D2 해소)** — generation_v1 §"depth self-check" 를 *이 facet 1축*
   범위로 축약한 1~2줄: "이 facet 의 layer 를 CONTEXT 가 받치는 만큼 다 펼쳤나, 값은 verbatim+
   단위인가, 조건/ITAAC 을 빠뜨리지 않았나, 한계를 inline 으로 표기했나."

> P5 net-neutral: generation_v1 의 "전체 answer_structure 골격화"(§"Logical structure")·"결론/
> 요약 섹션 금지"·전체-답 self-check 행은 슬롯에 불필요 → 제외해 길이 상쇄.

### 3.2 `_render_slot_prompt` — `# ANSWER SPEC`(전역) 동봉 (C3 해소)

단일-경로처럼 *전역* spec 요지를 슬롯에 전달해, answer_structure 가 인코딩한 단계별 sub-facet
심도 지시를 슬롯이 보게 한다. `# THIS SECTION`(이 슬롯) 은 유지하되, 그 *위에* `_render_spec_block`
의 축약(intent·answer_structure·governing_normative_class·전체 슬롯 1줄 목록)을 `# ANSWER SPEC`
로 싣는다. 슬롯이 "내가 전체 중 어느 단계이고 그 단계가 어떤 sub-facet 을 펼쳐야 하는지" 인지.

- 배치(현행 recency 규약 유지): `[본문][CITATION CONTRACT][# ANSWER SPEC(전역)][# PRIOR
  SECTIONS][# CONTEXT 서브셋][# THIS SECTION][# QUERY][lang]`.
- `_render_spec_block` 은 모듈 함수(`spec_driven_v1.py:1245`)라 import 해 재사용(중복 금지).

### 3.3 `_plan_slots` — 슬롯 CONTEXT 상한만 상향, **억지 보충 안 함** (C1, 사용자 결정 정정)

> **사용자 정정:** 무관 청크를 억지로 채우면 오인용·환각을 부른다 → *점수상위 보충
> (`slot_min_context`)은 두지 않는다.* 슬롯은 **자기 귀속 청크만** 본다(현행 유지). 귀속이
> 전무한 슬롯에만 빈 CONTEXT 를 막는 결정론 fallback(score 상위 K, 현행)을 남긴다.

따라서 C1 에 대한 변경은 **상한 `slot_context_k` 6→12 상향 1건뿐**이다. 귀속 청크가 6개를
넘던 슬롯이 잘리던 것을 풀어 *진짜 관련 근거*를 더 보게 한다(억지 삽입 아님). 굶음(귀속 0)
슬롯은 fallback 으로 빈 출력만 막고, 근거가 얕으면 그 한계를 정직하게 `근거 부족` 으로
표기하게 둔다(P1 — 억지 채움보다 정직한 한계가 낫다).

```python
# _plan_slots — 귀속만(보충 없음), 상한 캡. 귀속 0 일 때만 fallback(빈 출력 방지).
owned = [c for c in by_score if s.name in slots_by_chunk.get(c.chunk_id, set())]
attributed = len(owned)
fallback = not owned
owned = (by_score if fallback else owned)[: self._slot_context_k]   # 상한 12.
```

- slot_pin 에 `attributed_chunks`/`fallback_context` 기록(진단 — 어느 슬롯이 굶었는지 가시화).
- "근거 부족·인용↓" 증상은 **억지 보충이 아니라** ① 상한 상향(귀속 많은 슬롯), ② 전문 PRIOR
  로 슬롯 간 맥락 공유(§3.4), ③ 프롬프트의 "부축 생략 금지"(§3.1)로 완화한다 — 굶은 슬롯의
  근거 자체를 늘리는 건 *앞단(N1 슬롯 분해·N2 쿼리·N3 검색)* 의 몫이다(후속 §6).
- context_hash 는 전체 pack 기준이라 불변(`composer.py:499`) — 서브셋은 파생.

### 3.3b 슬롯 위계·헤더 — 가독성(전체 답에 `##` 한 레벨만, 본문은 헤더 금지)

> **사용자 보고:** 슬롯마다 독립 출력이라 모두 `#` 제목으로 시작 → 위계가 깨지고 가독성↓.

원인: 헤더는 `_plan_slots` 가 `## {label}` 로 결정론 prefix 하는데(`composer.py:555`), 슬롯
본문 프롬프트가 헤더 금지를 명시하지 않아 모델이 본문에서 또 `#`/`##` 를 내 **중복 헤더**가
생긴다. 또 슬롯이 *전체 답의 어느 단계*인지 몰라 독립 문서처럼 쓴다. 수정:

1. **헤더는 결정론으로 한 곳에서만** — `_plan_slots` 의 `## {label}` prefix 만 헤더를 낸다.
   전체 답의 헤더 레벨이 `##` 로 통일돼 위계가 일관(상위 `#` 없음, 종합 닫음 블록도 `##`).
2. **본문 헤더 금지(프롬프트)** — `composer_slot_v1.md` §"Output format" 에 "`#`/`##`/`###`
   제목을 내지 말고 본문만, 선두 빈 줄·구획명 반복 없이 곧바로 시작" 명시.
3. **결정론 backstop** — `_strip_leading_heading`(신규)이 본문 *선두* 연속 헤더 라인을 제거.
   라이브 스트리밍이라 화면은 못 되돌리나(모드 A), 기록 `answer_text`·PRIOR 전달은 정리되고,
   프롬프트 금지가 화면 중복을 1차로 막는다. divergence(모델이 헤더를 낸 드문 경우 화면>기록)
   는 주석에 명시(`composer.py:588`, 원칙 6 — 숨기지 않음).
4. **위계 인지(프롬프트 입력)** — `# THIS SECTION` 에 `단계: N / 총 M 구획 중` 을 싣고,
   `# ANSWER SPEC`(전역, §3.2)으로 전체 구조를 보여 슬롯이 *독립 문서가 아니라 한 단계*임을
   인지시킨다.

### 3.4 PRIOR SECTIONS — 이전 섹션 *전문* 통째 전달 (C2, 사용자 결정)

현행 요지 digest(슬롯명+첫 문장+cite-ID 한 줄) 대신 **앞 슬롯들의 *전문 텍스트*** (이미 화면에
스트리밍된 본문, 헤더 제외)를 PRIOR SECTIONS 로 전달해, 다음 슬롯이 앞 내용에 자연스럽게
이어쓰게 한다(분할감 해소 — 증상 3).

- `_generate_slotwise` 의 슬롯 루프에서 `digest_lines` 대신 `slot_outputs` 의 *헤더+전문*을
  누적해 다음 슬롯 `_render_slot_prompt(prior_sections=...)` 로 넘긴다(이미 화면에 스트리밍된 그
  본문과 동일 텍스트 — `streamed_parts` 와 동형).
- `_render_slot_prompt` 의 PRIOR SECTIONS 블록 문구를 *전문 동봉* 에 맞게 갱신:
  - **연결**: "앞 구획 전문이다 — 이 흐름을 이어 자연스럽게 연결하라."
  - **중복 금지**: "단, 앞 구획이 이미 확립한 사실을 *재서술·재인용* 하지 말고, 이 구획의
    facet 이 책임지는 *새 substance* 를 전개하라."
  - **근거 격리 불변(P1)**: "PRIOR 는 연결용 맥락이지 *근거가 아니다* — 모든 [cite-N] 은 이
    구획 CONTEXT 에서만. PRIOR 의 cite 를 그대로 베끼지 말 것."
- 구현: 신규 헬퍼 `_prior_sections_block(slot_outputs)` 가 헤더를 뺀 본문 전문을 `### [슬롯명]`
  라벨로 조립(요지 아님). 슬롯 루프·`_verify_slot`·`_regenerate_slot` 의 `prior_digest`
  파라미터를 `prior_sections` 로 교체.
- **토큰**: 전문 누적이라 뒤 슬롯일수록 프롬프트↑ → §3.5 max_tokens 상향과 짝. 긴 답이면
  PRIOR 가 과대해질 수 있어, *직전 K개 전문 + 그 이전은 한 줄 요지* 로 떨어뜨리는
  `prior_full_k` tunable 을 둔다(기본 None=전체 — 사용자 결정. 폭주 시 K 하향 안전판).
- 재현: PRIOR 전문이 rendered_prompt_hash 에 들어가므로(`composer.py:549`) 슬롯 핀 해시가
  앞 슬롯 출력에 의존 — 순차 실행이므로 결정론 유지(같은 입력 → 같은 체인).

### 3.5 `registry.yaml` 슬롯 max_tokens 상향 (C4 해소)

`composer_slot_v1` `model_options.max_tokens` 3000 → **8192** (단일-경로 16384 의 절반). 슬롯
CONTEXT 확대(§3.3) + 이전 섹션 전문 PRIOR(§3.4)로 입출력이 함께 늘어 6144 로도 절단 위험 →
8192. `_slot_max_tokens` 기본(`composer.py:88`)·tunable(`composer_slot_max_tokens`, `:1191`)도
동기. 256K 윈도우에서 입력 예산과 합산해도 여유.

> `_slot_model_options`(1134)는 `min(opts.max_tokens, self._slot_max_tokens)` 캡 — 둘 다
> 8192 로 올려야 실효(한쪽만 올리면 캡에 막힘).

### 3.6 composer_synthesize_v1.md — 현행 유지(범위 밖)

종합(닫음 블록)은 *본문 재출력 금지·정리+다음액션*이라 본 계획의 "정보 심도" 대상이 아니다.
본문 심도는 슬롯에서 나오므로 종합은 손대지 않는다(P5 — 무의미한 확장 금지). 단, 슬롯이
깊어지면 종합의 "cross-section tension/gap" 입력 품질이 자동 향상.

---

## 4. 영향·재현성

| 항목 | 변화 |
|---|---|
| 증상 1·2(근거 부족·인용↓) | §3.3 상한 6→12(귀속 많은 슬롯이 더 봄) + §3.1 부축 생략 금지 + §3.4 전문 PRIOR 맥락 공유. *억지 보충은 안 함*(사용자 결정 — 무관 청크는 오인용). 굶은 슬롯의 근거 증대는 앞단(§6). |
| 증상 3(인위적 분할) | §3.3b 헤더 결정론 단일화(`##` 한 레벨)+본문 헤더 금지+위계 인지 + §3.4 전문 PRIOR 연결 + §3.1 분업 규약 재서술. |
| 가독성(`#` 중복 제목) | §3.3b — `_strip_leading_heading` backstop + 프롬프트 헤더 금지 + `_plan_slots` 단일 헤더. |
| 프롬프트 sha256 | `composer_slot_v1.md` 재작성 → sha256 갱신(registry `:180`). 동기 필수(불일치 시 로드 실패). |
| rendered_prompt_hash | `_render_slot_prompt` 출력 변경(ANSWER SPEC·전문 PRIOR) → slot_pin.rendered_prompt_hash 자동 변경(`composer.py:588`)·combined_hash(`:646`) 변경 — 재현 핀 정상 갱신. PRIOR 전문 의존이라 슬롯 해시가 앞 슬롯 출력에 의존(순차 결정론 — 같은 입력 같은 체인). |
| prompt_profile_id | `composer_generation_slotwise_v1` 불변(`:671`). |
| L0 게이트 | 서브셋 확대 시 allowed_cites 확대 → 범위밖 cite 제거↓ → 위반↓. 게이트 로직 불변. |
| 기존 variant | spec_driven_v1·generation_v1 불변 → 회귀 0. |
| 토큰 비용 | 상한↑ + max_tokens↑ + 전문 PRIOR 누적 → 슬롯당·뒤 슬롯일수록 비용↑. `slot_context_k`/`prior_full_k`/max_tokens tunable 로 조절. |

---

## 5. 작업 순서 (구현 완료 — 2026-06-17)

1. ✅ **`_plan_slots` 상한 상향(§3.3)** — `slot_context_k` 6→12, **억지 보충 안 함**(사용자
   정정), slot_pin 에 `attributed_chunks` 기록.
2. ✅ **슬롯 위계·헤더(§3.3b)** — `_strip_leading_heading` backstop + `# THIS SECTION` 에
   `단계 N/총 M` + 프롬프트 헤더 금지.
3. ✅ **전문 PRIOR(§3.4)** — `_prior_sections_block`(헤더 뺀 전문) 신규, 슬롯 루프·검수·재생성
   의 `prior_digest`→`prior_sections` 교체, `prior_full_k` tunable.
4. ✅ **`# ANSWER SPEC` 동봉(§3.2)** — `_render_spec_block` import + 슬롯 프롬프트에 전역 spec.
5. ✅ **`composer_slot_v1.md` 재작성(§3.1)** — CORPUS/authority/주축+부축/표/self-check/헤더
   금지 이식. sha256 갱신·registry 동기, 로더 검증 통과.
6. ✅ **`registry.yaml`/tunable(§3.5)** — slot max_tokens 3000→8192, `_slot_max_tokens`/
   `slot_context_k` 기본·tunable 동기. 단위 테스트 459 통과.
7. ⬜ **실측 검증(다음)** — 다층 질의(GDC 35 단일고장·50.46(b) 5기준·RPV 화학조성)로 (a) 슬롯당
   인용 청크 수↑, (b) "근거 부족" 표기↓, (c) 구획 간 연결 자연스러움, (d) `#` 중복 제목 사라짐,
   (e) generation_v1 동등 심도. slot_pin 의 `attributed_chunks`/`fallback_context` 로 굶은
   슬롯 가시화.

---

## 6. 미해결·후속

- **굶은 슬롯의 근거 부족은 앞단 문제** — 억지 보충을 안 하므로(사용자 결정), 귀속이 0~2개인
  슬롯은 여전히 얕다. 근본 해소는 *N1 슬롯 분해 정밀화·N2 쿼리·N3 검색*에서 그 슬롯에 맞는
  청크를 더 건지는 것 — 본 변경 범위 밖. 검증서 굶은 슬롯이 잦으면 앞단 후속 계획으로.
- **전문 PRIOR 토큰 폭주** — 슬롯 多 + 각 구획 긴 답이면 뒤 슬롯 프롬프트가 누적 전문으로
  비대. `prior_full_k`(직전 K개만 전문, 그 외 한 줄 요지)로 안전판 — 기본은 전체(사용자 결정),
  폭주 관측 시 K 하향.
- **라이브 스트리밍 헤더 divergence** — 모드 A(라이브)라 모델이 본문 선두 헤더를 내면 화면은
  못 되돌리고 기록만 정리된다(`_strip_leading_heading`). 프롬프트 금지가 빈도를 줄이지만 0은
  아님 — 검증서 화면에 중복 헤더가 남으면 모드 B(검수 후 스트리밍) 전환 검토.
- **facet 없는 슬롯** — `facet` 이 비면 슬롯 프롬프트의 facet→축 매핑이 안 걸림. generation_v1
  처럼 *4축 전체*로 fallback 하는 분기를 슬롯 프롬프트에 둘지 검토.
- **L1 entailment(opt-in) 와의 상호작용** — 심도·전문 PRIOR 가 늘면 unsupported 판정·재생성
  빈도 변화 관찰 필요(`_verify_slot` slot_verify=l1).
