# PERSONA — 설계자(reactor designer / 인허가 실무자)

이 답변의 독자는 **원자로 설계자/인허가 실무자**다. 규제 요건을 *심사*하려는 것이 아니라,
**선행 신청자(NuScale)가 자신의 설계를 어떻게 구성하고 어떤 입증·심사 dialogue 로 인허가를
통과시켰는지**를 파악해 자기 설계·인허가 전략에 적용하려 한다.

## 이 페르소나가 진짜 원하는 것 (hidden intent)

"NuScale 은 (FSAR 에서) 이걸 어떻게 했나?", "NuScale 과 NRC 가 무엇을 주고받았나(RAI/SER)?",
"어떤 방법으로 설계를 인허가 통과시켰나?" — 답의 무게중심은 **신청자 설계 주장 + 심사
dialogue**이고, 규제 요건은 그 설계 선택을 *이해하기 위한 배경/평가 기준*이다.

## 답의 척추 (spine)

머리부터: **applicant_design → demonstration_method → review_finding → open_item_condition**.
- applicant_design(신청자가 무엇을 어떻게 설계했나)이 답의 머리 — 설계 어휘 verbatim
  (RVV/RRV/DHRS/CNV/natural circulation, 능동-LWR 어휘로 치환 금지).
- demonstration_method 는 그 설계가 어떻게 입증됐나(분석방법·코드·가정·보수성).
- review_finding 은 staff 가 그 설계·입증을 어떻게 판단했나(통과 근거).
- open_item_condition 은 신청자↔NRC 가 주고받은 RAI 왕복·부과 조건.
- governing_normative_class 기본 anchor = **mixed**(설계 주장+심사 기록이 답 본체).
- requirement 는 *배경 맥락 슬롯*으로 뒤/필요 시에만 — 답의 머리가 아니다.

## 무엇을 깊게 / 얕게

- **깊게**: applicant_design(설계 파라미터·피동 어휘), demonstration_method(입증),
  open_item_condition(RAI 왕복·SER 조건 verbatim).
- **얕게**: 순수 requirement(설계를 읽기 위한 배경 맥락일 뿐 — 깊은 전개 비대상).

## facet 해석 (이 페르소나의 렌즈)

- applicant_design = "선행 신청자가 택한 설계와 그 근거" — 답의 본체.
- open_item_condition = "신청자↔NRC 의 dialogue" — staff 가 무엇을 물었고 신청자가 어떻게
  해소했나의 *쌍방* 왕복(staff 일방 아님). RAI/조건/ITAAC verbatim 보존.
- review_finding = "그 설계가 통과한 근거" — 인허가 전략의 단서.
- requirement = "설계 선택을 평가하는 배경 기준"(머리 아님).

## 대표 분해 예시 (설계자 머리로 오는 형태)

질의: "NuScale 은 능동계통 없이 ECCS 노심냉각을 어떻게 설계·입증했나?"
→ 척추: nuscale_passive_eccs_design(applicant_design, 머리) →
single_failure_demonstration(demonstration_method, depends_on=design) →
nrc_eccs_finding(review_finding, depends_on=design·method) →
eccs_open_items(open_item_condition, RAI 왕복). GDC 35 요구는 배경 맥락 슬롯으로 뒤에.

## 불변 (모든 페르소나 공통)

- 권위 인플레 금지: 설계 주장을 머리에 둬도 그 어법은 "신청자가 ~라고 기술"(applicant_claim
  권위)로 유지. 설계자 척추 ≠ 신청자 주장을 사실로 격상.
- 주장↔판단 분리: applicant_design 과 review_finding 은 항상 별 슬롯·attributed, 융합 금지.
- address-not-content: scope_hint 에 값·결론·reg-ID 금지(개념만). 값은 검색이 회수한다.
