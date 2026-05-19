from __future__ import annotations

from app.domain.retrieval import RetrievedChunk

# 기획 doc §4 Citation Format 3종:
#   노형 문서: [노형명 DC Tier 2, Chapter X.Y.Z, p. P, Rev. R]
#   규제 문서: [규제 ID, Section X.Y, p. P, Rev. R (YYYY)]
#   RAI 문서: [노형 RAI #NNNN, Response p. P, YYYY-MM-DD]

VENDOR = "vendor"
REGULATION = "regulation"
RAI = "rai"


def infer_doc_type(document_id: str | None, fallback: str | None = None) -> str:
    """document_id 접두/패턴으로 doc_type 추정.

    시드 문서 컨벤션:
      kins-*, rg-*, 10cfr-*, nureg-*       → regulation
      rai-*, *-rai-*                         → rai
      design-spec-*, nuscale-*, ismr-*, ...  → vendor
    """
    if fallback in (VENDOR, REGULATION, RAI):
        return fallback
    if not document_id:
        return VENDOR
    did = document_id.lower()
    if did.startswith(("kins-", "rg-", "rg1.", "10cfr", "nureg")) or "regulation" in did:
        return REGULATION
    if "rai" in did:
        return RAI
    return VENDOR


def format_citation(chunk: RetrievedChunk, citation_id: str) -> str:
    """기획 doc §Citation Format 그대로 적용.

    데이터 결손 필드는 명시적으로 "?"로 표기해 verification이 잡을 수 있게 한다.
    """
    doc_type = chunk.doc_type or infer_doc_type(chunk.document_id)
    doc = chunk.document_id or "?"
    page = chunk.page if chunk.page is not None else "?"
    section = chunk.section or "?"
    rev = chunk.revision or "?"

    if doc_type == REGULATION:
        # [{doc}, Section {section}, p. {page}, Rev. {rev}]
        return f"[{citation_id}] [{doc}, Section {section}, p. {page}, Rev. {rev}]"
    if doc_type == RAI:
        date = chunk.response_date or "?"
        return f"[{citation_id}] [{doc}, Response p. {page}, {date}]"
    # vendor (default)
    return f"[{citation_id}] [{doc}, Chapter {section}, p. {page}, Rev. {rev}]"
