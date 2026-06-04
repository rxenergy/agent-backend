from __future__ import annotations

import pytest

from app.application.classification.rule import RuleClassifier


@pytest.mark.asyncio
async def test_vendor_technical_is_o1_d2() -> None:
    r = await RuleClassifier().classify("NuScale의 PCS 설계 특징은? 메커니즘 수치 포함")
    assert r.scenario_object == "O1"
    assert r.scenario_depth == "D2"
    assert "NuScale" in r.entities.get("vendors", [])
    assert r.confidence > 0


@pytest.mark.asyncio
async def test_regulation_formal_is_o2_d3() -> None:
    r = await RuleClassifier().classify("RG 1.157의 원문 정의를 그대로 알려줘")
    assert r.scenario_object == "O2"
    assert r.scenario_depth == "D3"
    assert "RG 1.157" in r.entities.get("regulation_ids", [])


@pytest.mark.asyncio
async def test_rai_technical_is_o3_d2() -> None:
    r = await RuleClassifier().classify("RAI #1234에서 다룬 설계 메커니즘과 수치는?")
    assert r.scenario_object == "O3"
    assert r.scenario_depth == "D2"
    assert any("1234" in s for s in r.entities.get("rai_numbers", []))


@pytest.mark.asyncio
async def test_relation_when_multiple_objects_present() -> None:
    r = await RuleClassifier().classify(
        "NuScale이 RG 1.157을 어떻게 만족하는지 설계 메커니즘"
    )
    # vendor + regulation 동시 등장 + "어떻게 만족" → O4 Relation
    assert r.scenario_object == "O4"


@pytest.mark.asyncio
async def test_unknown_falls_back_to_default_with_zero_confidence() -> None:
    r = await RuleClassifier().classify("안녕")
    assert r.confidence == 0.0
    assert r.scenario_object in ("O1", "O2", "O3", "O4")
    assert r.scenario_depth in ("D1", "D2", "D3")


# --- goldset 계획 W-A/W-B/W-C 커버리지 회귀 (코퍼스 실재 식별자) ---


@pytest.mark.asyncio
async def test_gdc_is_regulation_o2() -> None:
    # GDC(10 CFR 50 App A)는 규제 → O2, entity 로 추출.
    r = await RuleClassifier().classify("GDC 35(비상 노심 냉각)의 기술적 특성은 무엇인가?")
    assert r.scenario_object == "O2"
    assert "GDC 35" in r.entities.get("regulation_ids", [])


@pytest.mark.asyncio
async def test_srp_review_criteria_is_o2_not_o3() -> None:
    # "심사 기준"(SRP 심사지침)이 더 이상 O3 로 새지 않는다(D-1).
    r = await RuleClassifier().classify("SRP Chapter 5.2.3(RCPB 재료)의 심사 기준은?")
    assert r.scenario_object == "O2"
    assert any("SRP" in s for s in r.entities.get("regulation_ids", []))


@pytest.mark.asyncio
async def test_rai_code_is_o3_d3() -> None:
    # 코드형 RAI 식별자(DWO-SC-22) + 한국어 조사 뒤에서도 추출.
    r = await RuleClassifier().classify("DWO-SC-22의 공식 질의문 전문은?")
    assert r.scenario_object == "O3"
    assert r.scenario_depth == "D3"
    assert "DWO-SC-22" in r.entities.get("rai_numbers", [])


@pytest.mark.asyncio
async def test_audit_query_code_is_o3() -> None:
    r = await RuleClassifier().classify(
        "SDAA 감사 질의 A-5.2.1.1-4는 어떤 ASME 코드 적용 문제를 다루는가?"
    )
    assert r.scenario_object == "O3"
    assert "A-5.2.1.1-4" in r.entities.get("rai_numbers", [])


@pytest.mark.asyncio
async def test_cfr_subsection_entity_extracted() -> None:
    r = await RuleClassifier().classify("10 CFR 50.46(a)(1)(i) 원문(최적추정 평가 모델 요건)은?")
    assert r.scenario_object == "O2"
    assert r.scenario_depth == "D3"
    assert "10 CFR 50.46(a)(1)(i)" in r.entities.get("regulation_ids", [])


@pytest.mark.asyncio
async def test_pdc_extracted_neutral_not_regulation() -> None:
    # PDC(노형 설계기준)는 design_criteria 로만 추출, regulation_ids 미합류(Q-4).
    r = await RuleClassifier().classify("NuScale FSAR에서 PDC 34(잔열 제거) 인용 문구는?")
    assert "PDC 34" in r.entities.get("design_criteria", [])
    assert not any("PDC" in s for s in r.entities.get("regulation_ids", []))
