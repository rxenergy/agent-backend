from __future__ import annotations

import re

from app.domain.retrieval import RetrievedChunk

# NRC ADAMS accession number(ML번호) → 정적 공개 PDF 딥링크.
# 검증된 패턴(nrc.gov 자체 서빙 URL): ML18002A422 →
#   https://www.nrc.gov/docs/ML1800/ML18002A422.pdf
# 폴더 = "ML" + accession 의 첫 4자리. 정적 /docs/ 경로라 WBA API 폐기(2026-06-30)와 무관.
# 앵커(^…$)해 non-ADAMS document_id(source_id 안의 ML-부분열) 오매칭을 막는다.
_ADAMS_RE = re.compile(r"^ML(\d{4})\w+$")


def adams_url(document_id: str | None) -> str | None:
    """ADAMS 형식 document_id → 공개 PDF URL. 비-ADAMS(RG-/KEPIC-/kins- 등)는
    None → 호출측이 평문 fallback. 무근거 링크 404 를 만들지 않는다."""
    if not document_id:
        return None
    m = _ADAMS_RE.match(document_id)
    if not m:
        return None
    return f"https://www.nrc.gov/docs/ML{m.group(1)}/{document_id}.pdf"

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
