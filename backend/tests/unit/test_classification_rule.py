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
