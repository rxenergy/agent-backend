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

# ── 규범적 무게(normative weight) ──────────────────────────────────────────
# 출처의 *권위 등급*. `authority_tier`(검색 hard gate, primary/secondary/tertiary)
# 와 직교한다 — 구속 10CFR 과 비구속 RG 가 둘 다 primary 일 수 있다. 답변이 인용을
# 어떤 권위로 서술할지(의무인가·권고인가·주장인가)의 표현 신호이며, doc_type/
# collection/clause_id/document_id 에서 *결정론적*으로 파생한다(LLM 아님 — 권위는
# 출처 속성, [[model_over_rule]] 결정=코드). retriever_opensearch._derive_authority_tier
# 의 collection 매핑 패턴과 평행. (계획: prompt_regulatory_authority_expressiveness.plan.v1 W-E)
BINDING = "binding"                    # 구속 요건: 10 CFR·GDC·고시 — "shall/must"
GUIDANCE = "guidance"                  # 권고·비구속: RG·SRP·DSRS — "one acceptable method"
REVIEW_RECORD = "review_record"        # 심사 기록: SER·RAI·Audit — 특정 사건의 NRC 판단
APPLICANT_CLAIM = "applicant_claim"    # 신청자 주장: FSAR·미승인 TR — 검증 전 주장
APPROVED_PRECEDENT = "approved_precedent"  # 인증 선례: 승인 TR·인증 설계 — 설득적 선례
GENERAL_KNOWLEDGE = "general_knowledge"    # 일반 지식: 코퍼스 밖 — 비근거·비권위

# 출처 라인 한국어 태그(W-D — dumb client 도 권위를 본다, 원칙 8).
_WEIGHT_LABEL_KO = {
    BINDING: "구속 요건",
    GUIDANCE: "권고·비구속 지침",
    REVIEW_RECORD: "심사 기록",
    APPLICANT_CLAIM: "신청자 주장",
    APPROVED_PRECEDENT: "인증 선례",
    GENERAL_KNOWLEDGE: "일반 지식·비근거",
}

# collection(= RetrievedChunk.doc_type, OpenSearch fine-grained) → 규범무게.
# 미지 collection 은 None → document_id/clause 접두 fallback 으로 강등(아래).
_WEIGHT_BY_COLLECTION = {
    "10CFR": BINDING,
    "FR": BINDING,        # Federal Register 법제화분
    "RG": GUIDANCE,
    "SRP": GUIDANCE,
    "DSRS": GUIDANCE,
    "NUREG": GUIDANCE,
}


def weight_label(weight: str) -> str:
    """규범무게 코드 → 출처 라인 한국어 태그. 미지 무게는 그대로 반환."""
    return _WEIGHT_LABEL_KO.get(weight, weight)


def normative_weight(chunk: RetrievedChunk) -> str:
    """chunk 의 출처 권위(규범무게)를 결정론적으로 파생.

    신호 우선순위(robust — collection 이 coarse·부재인 경로에서도 분리):
      1. collection / doc_type (OpenSearch fine-grained: 10CFR/RG/SRP/DSRS/nuscale_*)
      2. clause_id 접두 (10CFR50.46 / GDC* → binding; RG_* → guidance)
      3. document_id 접두 (infer_doc_type 가 이미 매칭하는 패턴 확장)
    어느 신호도 권위를 가르지 못하면 보수적으로 약등(권위 인플레이션 회피).
    승인 표식(approved_precedent)은 시드 메타에 없으면 applicant_claim 으로 보수
    강등한다(silent 격상 금지 — 계획 Q-1).
    """
    # 1. collection / fine-grained doc_type
    coll = chunk.collection or chunk.doc_type
    if coll:
        if coll in _WEIGHT_BY_COLLECTION:
            return _WEIGHT_BY_COLLECTION[coll]
        cl = coll.lower()
        if "rai" in cl or "ser" in cl or "audit" in cl:
            return REVIEW_RECORD
        if cl.startswith("nuscale") or "vendor" in cl:
            return APPLICANT_CLAIM

    # 2. clause_id 접두 (조문 ID — exact 신호)
    clause = (chunk.clause_id or "").upper().replace(" ", "")
    if clause.startswith("10CFR") or clause.startswith("GDC"):
        return BINDING
    if clause.startswith("RG"):
        return GUIDANCE

    # 3. document_id 접두 fallback
    did = (chunk.document_id or "").lower()
    if did:
        if "rai" in did or "-ser" in did or "_ser" in did or "audit" in did:
            return REVIEW_RECORD
        if did.startswith(("10cfr", "gdc")):
            return BINDING
        if did.startswith(("rg-", "rg1.", "rg_", "srp", "dsrs", "nureg")):
            return GUIDANCE
        if "regulation" in did:
            # coarse '규제'인데 구속/권고를 가를 신호가 없음 → 인플레이션 회피 위해
            # 권고로 약등(의무 단정보다 안전).
            return GUIDANCE
        if did.startswith(("kins-", "kins_")):
            # KINS 고시(구속)/지침(권고) 미구분 → 보수적 권고(거짓 의무 회피, Q-2).
            return GUIDANCE
        if did.startswith(("nuscale", "ismr", "i-smr", "design-spec")) or "fsar" in did:
            return APPLICANT_CLAIM

    # 4. coarse doc_type 최종 fallback
    dt = (chunk.doc_type or "").lower()
    if dt == RAI:
        return REVIEW_RECORD
    if dt == REGULATION:
        return GUIDANCE
    if dt == VENDOR:
        return APPLICANT_CLAIM

    return GENERAL_KNOWLEDGE


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
    # doc_type 은 인용 *형식*(Section vs Chapter vs Response)을 고른다. 그러나
    # format 의 coarse 3분류(vendor/regulation/rai)는 *권위*를 가르지 못하므로,
    # 권위는 별도 normative_weight(chunk)(collection/clause/document_id 파생)로 구해
    # 출처 라인에 태그한다 — RG(권고)와 10CFR(구속)를 같은 'regulation' 형식으로
    # 쓰되 무게는 분리(W-D).
    # 인용 형식(Section/Chapter/Response)을 규범무게에서 정합시킨다 — fine-grained
    # collection(10CFR/RG)이나 ADAMS document_id(ML…)에서도 규제는 Section 형식으로
    # 나오게(coarse infer_doc_type 의 vendor 오분류 회피).
    weight = normative_weight(chunk)
    if weight in (BINDING, GUIDANCE):
        doc_type = REGULATION
    elif weight == REVIEW_RECORD:
        doc_type = RAI
    elif weight in (APPLICANT_CLAIM, APPROVED_PRECEDENT):
        doc_type = VENDOR
    else:
        doc_type = chunk.doc_type if chunk.doc_type in (VENDOR, REGULATION, RAI) \
            else infer_doc_type(chunk.document_id)
    doc = chunk.document_id or "?"
    page = chunk.page if chunk.page is not None else "?"
    section = chunk.section or "?"
    # revision 결손은 "Rev. ?"로 강제하지 않고 *생략*한다 — 없는 개정 번호를 지어낸
    # 듯한 깨진 토큰("Rev. ?")을 출처 라인에 남기지 않기 위함. (page/section 은 형식
    # 골격이라 "?"를 유지해 verification 이 결손을 잡게 둔다.)
    rev = (chunk.revision or "").strip()
    rev_part = f", Rev. {rev}" if rev else ""
    tag = f" ({weight_label(weight)})"

    if doc_type == REGULATION:
        # [{doc}, Section {section}, p. {page}{, Rev. {rev}}]
        return f"[{citation_id}] [{doc}, Section {section}, p. {page}{rev_part}]{tag}"
    if doc_type == RAI:
        date = chunk.response_date or "?"
        return f"[{citation_id}] [{doc}, Response p. {page}, {date}]{tag}"
    # vendor (default)
    return f"[{citation_id}] [{doc}, Chapter {section}, p. {page}{rev_part}]{tag}"
