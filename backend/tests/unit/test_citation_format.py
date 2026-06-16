from __future__ import annotations

from app.application.context.citation_format import (
    APPLICANT_CLAIM,
    BINDING,
    GENERAL_KNOWLEDGE,
    GUIDANCE,
    RAI,
    REGULATION,
    REVIEW_RECORD,
    VENDOR,
    cfr_section_label,
    format_citation,
    infer_doc_type,
    normative_weight,
)
from app.domain.retrieval import RetrievedChunk


def _chunk(**kw) -> RetrievedChunk:
    base = dict(chunk_id="ch", document_id="doc", score=0.9)
    base.update(kw)
    return RetrievedChunk(**base)


def test_infer_regulation_by_prefix() -> None:
    assert infer_doc_type("kins-rg-2024-001") == REGULATION
    assert infer_doc_type("rg-1-157") == REGULATION
    assert infer_doc_type("10cfr50-46") == REGULATION
    assert infer_doc_type("nureg-0800") == REGULATION


def test_infer_rai_by_substring() -> None:
    assert infer_doc_type("nuscale-rai-1234") == RAI
    assert infer_doc_type("rai-2018-05") == RAI


def test_infer_vendor_default() -> None:
    assert infer_doc_type("design-spec-pwr-smr-2025") == VENDOR
    assert infer_doc_type("nuscale-dc-tier2") == VENDOR
    assert infer_doc_type(None) == VENDOR


def test_format_vendor() -> None:
    c = RetrievedChunk(
        chunk_id="ch1",
        document_id="nuscale-dc-tier2",
        score=0.9,
        page=45,
        section="6.2.3",
        revision="5",
        doc_type=VENDOR,
    )
    s = format_citation(c, "cite-0")
    assert "Chapter 6.2.3" in s
    assert "p. 45" in s
    assert "Rev. 5" in s


def test_format_omits_revision_when_absent() -> None:
    # revision 결손 시 "Rev. ?"를 강제로 남기지 않는다(깨진 토큰 회피).
    vendor = format_citation(
        RetrievedChunk(chunk_id="ch1", document_id="nuscale-dc-tier2", score=0.9,
                       page=6, section="(preamble) > #41", revision=None,
                       doc_type=VENDOR),
        "cite-2",
    )
    assert "Rev." not in vendor
    assert "p. 6]" in vendor  # page 뒤에서 바로 닫힌다.
    reg = format_citation(
        RetrievedChunk(chunk_id="ch2", document_id="rg-1-157", score=0.9,
                       page=12, section="4.2", revision="   ", doc_type=REGULATION),
        "cite-0",
    )
    assert "Rev." not in reg  # 공백만 있는 revision 도 생략.


def test_format_regulation() -> None:
    c = RetrievedChunk(
        chunk_id="ch1",
        document_id="rg-1-157",
        score=0.9,
        page=12,
        section="4.2",
        revision="3 (2017)",
        doc_type=REGULATION,
    )
    s = format_citation(c, "cite-0")
    assert "Section 4.2" in s
    assert "Rev. 3 (2017)" in s


def test_format_rai() -> None:
    c = RetrievedChunk(
        chunk_id="ch1",
        document_id="nuscale-rai-1234",
        score=0.9,
        page=8,
        section=None,
        response_date="2018-05-15",
        doc_type=RAI,
    )
    s = format_citation(c, "cite-0")
    assert "Response p. 8" in s
    assert "2018-05-15" in s


# ── 규범적 무게(normative weight) — W-E ──────────────────────────────────────

def test_weight_by_collection_fine_grained() -> None:
    # OpenSearch 경로: doc_type = collection(fine-grained).
    assert normative_weight(_chunk(doc_type="10CFR")) == BINDING
    assert normative_weight(_chunk(doc_type="FR")) == BINDING
    assert normative_weight(_chunk(collection="RG")) == GUIDANCE
    assert normative_weight(_chunk(collection="SRP")) == GUIDANCE
    assert normative_weight(_chunk(collection="DSRS")) == GUIDANCE
    assert normative_weight(_chunk(collection="nuscale_design")) == APPLICANT_CLAIM


def test_weight_clause_id_fallback() -> None:
    # collection 이 coarse('regulation')라 무게를 못 가를 때 clause 가 분리.
    assert normative_weight(
        _chunk(doc_type="regulation", clause_id="10CFR50.46")
    ) == BINDING
    assert normative_weight(
        _chunk(doc_type="regulation", clause_id="RG_1_157")
    ) == GUIDANCE
    assert normative_weight(_chunk(clause_id="GDC 35")) == BINDING


def test_weight_document_id_prefix_fallback() -> None:
    # collection·clause 부재 시 document_id 접두로 binding↔guidance 분리.
    assert normative_weight(_chunk(document_id="10cfr50-46")) == BINDING
    assert normative_weight(_chunk(document_id="rg-1-157")) == GUIDANCE
    assert normative_weight(_chunk(document_id="nuscale-rai-1234")) == REVIEW_RECORD
    assert normative_weight(_chunk(document_id="nuscale-dc-tier2")) == APPLICANT_CLAIM
    # kins- 는 고시(구속)/지침(권고) 미구분 → 보수적 권고(거짓 의무 회피).
    assert normative_weight(_chunk(document_id="kins-rg-2024-001")) == GUIDANCE


def test_weight_no_signal_is_general_knowledge() -> None:
    # 검색 무결과·fake local(doc_type 무) → 비근거.
    assert normative_weight(_chunk(document_id="")) == GENERAL_KNOWLEDGE


def test_format_appends_weight_tag() -> None:
    # 같은 'regulation' 형식이라도 RG(권고)와 10CFR(구속)의 무게가 출처에 분리된다.
    rg = format_citation(
        _chunk(document_id="rg-1-157", doc_type=REGULATION, section="4.2", page=12), "cite-0"
    )
    assert "Section 4.2" in rg
    assert "(권고·비구속 지침)" in rg
    cfr = format_citation(
        _chunk(document_id="10cfr50-46", doc_type=REGULATION, section="50.46", page=1), "cite-1"
    )
    assert "(구속 요건)" in cfr


# ── ADAMS PDF 딥링크(#page=N) ────────────────────────────────────────────────

def test_format_adams_deeplinks_with_page_anchor() -> None:
    # ADAMS(ML번호) 문서 + page → document_id 토큰이 [doc](url#page=N) markdown 링크.
    s = format_citation(
        _chunk(document_id="ML18002A422", doc_type=REGULATION, section="4.2", page=12),
        "cite-0",
    )
    assert (
        "[ML18002A422](https://www.nrc.gov/docs/ML1800/ML18002A422.pdf#page=12)"
        in s
    )
    # 출처 라인 골격(Section/p.)은 보존.
    assert "Section 4.2" in s
    assert "p. 12" in s


def test_format_adams_no_page_omits_anchor() -> None:
    # page 결손 시 #page 앵커 없이 링크만(잘못된 페이지로 보내지 않는다).
    s = format_citation(
        _chunk(document_id="ML18002A422", doc_type=REGULATION, section="4.2", page=None),
        "cite-0",
    )
    assert "[ML18002A422](https://www.nrc.gov/docs/ML1800/ML18002A422.pdf)" in s
    assert "#page=" not in s


def test_format_non_adams_stays_plaintext() -> None:
    # 비-ADAMS(RG-/KINS- 등)는 평문 document_id 유지(404 링크 회피).
    s = format_citation(
        _chunk(document_id="rg-1-157", doc_type=REGULATION, section="4.2", page=12),
        "cite-0",
    )
    assert "](http" not in s  # markdown 링크 없음.
    assert "[rg-1-157, Section 4.2" in s


# ── 10 CFR §조문 라벨 ─────────────────────────────────────────────────────────

def test_cfr_section_label_variants() -> None:
    assert cfr_section_label("10CFR50.46") == "10 CFR §50.46"
    assert cfr_section_label("10 CFR 50.46") == "10 CFR §50.46"
    assert cfr_section_label("10CFR50.55a") == "10 CFR §50.55a"
    assert cfr_section_label("10CFR100") == "10 CFR §100"
    assert cfr_section_label("10CFR50.34(f)") == "10 CFR §50.34(f)"
    # 비-CFR / 형식 불일치 → None(fallback).
    assert cfr_section_label("RG_1_157") is None
    assert cfr_section_label(None) is None
    assert cfr_section_label("") is None


def test_format_10cfr_uses_section_clause_label() -> None:
    # 10 CFR 은 packageId 대신 조문 ID(clause_id)로 "10 CFR §50.46" 표기 + Section/page.
    c = RetrievedChunk(
        chunk_id="ch1", document_id="CFR-2024-title10-vol1", score=0.9,
        page=512, section="50.46(b)(1)", collection="10CFR", clause_id="10CFR50.46",
    )
    s = format_citation(c, "cite-0")
    assert "[10 CFR §50.46, Section 50.46(b)(1), p. 512]" in s
    assert "(구속 요건)" in s
    # govinfo packageId 는 라벨에 노출되지 않는다(사람용 §조문 우선).
    assert "CFR-2024-title10-vol1" not in s


def test_format_10cfr_without_clause_id_falls_back_to_doc() -> None:
    # clause_id 부재 시 기존 동작(document_id 평문) 유지.
    c = RetrievedChunk(
        chunk_id="ch1", document_id="CFR-2024-title10-vol1", score=0.9,
        page=512, section="50.46", collection="10CFR", clause_id=None,
    )
    s = format_citation(c, "cite-0")
    assert "[CFR-2024-title10-vol1, Section 50.46" in s
