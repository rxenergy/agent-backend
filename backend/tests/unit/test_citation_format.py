from __future__ import annotations

from app.application.context.citation_format import (
    RAI,
    REGULATION,
    VENDOR,
    format_citation,
    infer_doc_type,
)
from app.domain.retrieval import RetrievedChunk


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
