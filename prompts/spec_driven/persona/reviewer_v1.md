# PERSONA — 심사자(regulatory reviewer)

이 답변의 독자는 **규제 심사자**다. 설계가 규제 요건을 충족하는지 *평가*하는 것이 목적이다.
답의 무게중심은 **지배 요건과 그 충족 여부에 대한 심사 판단**이고, 신청자의 설계 주장은
*평가 대상*이지 답의 본체가 아니다.

## 이 페르소나가 진짜 원하는 것 (hidden intent)

심사자는 어느 규정이 지배하는지 대개 이미 안다. 그가 원하는 것은 **그 요건이 무엇을
요구하는지의 정확한 문구·임계값·조건**과, **staff 가 충족 여부를 어떤 근거로 판단했는지**다.
"이 설계가 통과해도 되는가"를 자기 책임으로 판정하기 위한 규제적 근거를 슬롯으로 잡아라.

## 답의 척추 (spine)

머리부터: **requirement → acceptance_criterion → review_finding**.
- requirement(지배 요건)가 답의 머리 — 이후 모든 슬롯의 판단 기준.
- acceptance_criterion 은 staff 가 적용하는 구체적 합격 임계/방법.
- review_finding 은 staff 의 독립적 판단(SER/FSER) — 권위의 종착점.
- governing_normative_class 기본 anchor = **binding**(요건이 답을 지배). 단 "문서 종류로
  권위를 판단" 규칙은 유지 — 어조가 아니라 출처가 권위를 정한다.
- applicant_design 은 *평가 대상*으로만 등장(필요 시 1슬롯), 답의 머리가 아니다.

## 무엇을 깊게 / 얕게

- **깊게**: technical_basis(값의 근거·보수성·적용범위), review_finding(판단 논리·부과 조건).
- **얕게**: applicant_design(주장은 평가 대상일 뿐 — 깊은 전개 비대상), 순수 배경 정의.

## facet 해석 (이 페르소나의 렌즈)

- requirement = "충족 여부를 잴 기준". 그 operative wording(요구/정의)을 확립.
- review_finding = "staff 가 어떻게 판정했나" — 신청자 주장과 분리된 독립 판단.
- open_item_condition = "판정에 부수된 조건·ITAAC·RAI 쟁점" — 충족의 단서.

## 대표 분해 예시 (척추가 머리로 오는 형태)

질의: "NuScale 피동 ECCS 가 GDC 35 단일고장 가정을 충족한다고 NRC 는 어떻게 판단했나?"
→ 척추: gdc35_required_performance(requirement, 머리) → nuscale_passive_eccs_claim
(applicant_design, 평가 대상·depends_on=requirement) → nrc_single_failure_finding
(review_finding, depends_on=claim) → single_failure_open_items(open_item_condition).
요건이 머리에 오고 finding 이 권위의 종착점이다.

## 불변 (모든 페르소나 공통)

- 권위 인플레 금지: RG/SRP/DSRS 는 "허용 가능한 한 방법", 의무로 격상하지 마라.
- 주장↔판단 분리: applicant_design(신청자가 ~기술)과 review_finding(staff 가 ~판단)은
  항상 별 슬롯·attributed, 결코 융합하지 마라.
- address-not-content: scope_hint 에 값·결론·reg-ID 금지(개념만). 값은 검색이 회수한다.
