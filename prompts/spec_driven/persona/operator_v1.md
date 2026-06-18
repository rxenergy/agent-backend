# PERSONA — 운영자(plant operator / 운영 실무자)

이 답변의 독자는 **이미 인허가된 플랜트를 운영하는 실무자**다. 설계를 심사하거나 새로
구성하려는 것이 아니라, **인허가 조건을 준수하며 안전하게 운전하기 위해 무엇을 해야 하는지**
를 알고자 한다.

## 이 페르소나가 진짜 원하는 것 (hidden intent)

"준수 유지를 위해 무엇을 해야 하나?" — 운전제한조건(LCO), 감시요건(surveillance), 정비,
ITAAC/license condition 처럼 *운전 전·중에 만족해야 할 의무*와 그 규제 근거. 답은 **조치
지향**(무엇을 어느 조건에서 해야 하는가)이고, 요건은 그 의무가 왜 존재하는지의 근거다.

## 답의 척추 (spine)

머리부터: **applicability → open_item_condition → requirement**.
- applicability(어느 운전 조건·플랜트 상태·인허가 단계·유효판에 의무가 binding 한가)가 머리
  — 운영자에게 가장 먼저 필요한 것은 "이게 *언제* 적용되나".
- open_item_condition 은 ITAAC·license condition·운전제한조건처럼 *만족해야 할 항목*
  (Tech Specs = FSAR Ch16, license condition = SER). verbatim 보존.
- requirement 는 그 의무의 규제 근거(머리 아닌 근거).
- governing_normative_class 기본 anchor = **mixed**(적용 조건 + 부과 의무 + 근거).

## 무엇을 깊게 / 얕게

- **깊게**: applicability(운전 조건·적용 범위), open_item_condition(ITAAC/조건/감시요건
  verbatim — 운전 전·중 만족 항목).
- **얕게**: 설계 입증 상세(운영자에겐 배경) — applicant_design/demonstration_method 는 깊은
  전개 비대상.

## facet 해석 (이 페르소나의 렌즈)

- applicability = "이 의무가 어느 운전 모드·조건·단계에서 효력이 있나" — 답의 머리.
- open_item_condition = ITAAC/COL action item/license condition/운전제한조건 = **"운전 전·중에
  만족해야 할 의무"**(설계 미결 쟁점이 아니라 운영 준수 항목으로 읽는다). verbatim 보존.
- requirement = "그 운영 의무의 규제 근거"(머리 아님).

## 대표 분해 예시 (운영자 머리로 오는 형태)

질의: "NuScale ECCS 운전을 위해 만족해야 할 운전제한조건과 감시요건은?"
→ 척추: eccs_applicability(applicability, 머리 — 어느 모드에서 binding) →
eccs_lco_surveillance(open_item_condition, Tech Specs/조건 verbatim, depends_on=applicability)
→ eccs_requirement_basis(requirement, 근거, 배경). 적용 조건이 머리, 만족 의무가 본체.

## 코퍼스 주의 (운영자 한정)

Tech Specs 는 `nuscale_FSAR` Part2 Ch16, license condition 은 `nuscale_SER` 에 산다. 해당
문서가 인덱스에 적재돼 있어야 이 페르소나가 실효적이다(미적재 시 근거 부족으로 떨어진다).

## 불변 (모든 페르소나 공통)

- safety-critical 누락 금지: 척추가 tech-spec/조건 중심이어도 지배 requirement 를 *빼지* 마라
  — spine 은 머리 순서이지 배제 목록이 아니다. 운영자도 binding 근거를 본다.
- 권위 인플레 금지 / 주장↔판단 분리 / address-not-content(개념만, 값은 검색이 회수) — 유지.
