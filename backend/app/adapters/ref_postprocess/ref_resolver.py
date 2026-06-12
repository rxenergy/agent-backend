"""Rule-base 참조 해소기 — LLM 없이 raw 참조를 catalog source_id로 매핑.

LLM(1차 추출)이 뽑은 ``RawRef``(kind/identifier/section_path)를 받아, kind별
결정적 규칙으로 metadata catalog의 ``source_id`` 후보를 **신뢰도 점수와 함께
다건** 산출한다. 마지막에 OpenSearch ``terms`` 필터 절로 변환한다.

설계 노트:
- 카테고리 라우팅은 metadata.csv의 리스트값 ``DocumentType`` 컬럼만 사용한다
  (legacy 파생 컬럼 ``doc_category`` / 삭제된 camelCase ``documentType``은 미사용).
- RG/NUREG/FR/TR/SECY는 catalog rows 파생 역인덱스
  (:func:`build_report_number_index_from_catalog`,
  :func:`build_case_reference_index_from_catalog`)로 O(1) 조회.
- CFR/GDC/SRP/DSRS/ML은 catalog row를 직접 라우팅.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ref_catalog import (
    IndexEntry,
    RefCatalog,
    normalize_report_id,
    parse_cfr_part,
    part_in_range,
)


# kind ∈ 추출기가 분류하는 인용 종류
# NRC 규제문서(RG..ML) + NuScale 자체 문서(TR/FSAR/RAI/SECY) + 카탈로그 밖(OTHER)
VALID_KINDS = (
    "RG", "NUREG", "FR", "SRP", "DSRS", "CFR", "GDC", "ML",
    "TR", "FSAR", "RAI", "SECY", "OTHER",
)

# 한 문서 번호(예: NUREG-0800) 하나가 이 값보다 많은 서로 다른 문서를 동시에 가리키면,
# 그 번호만으로는 어느 문서를 뜻하는지 특정할 수 없다고 본다. 이 경우 섹션 번호로 좁히고
# (좁혀지면 채택), 좁힐 단서조차 없으면 그 참조를 필터에서 통째로 제외한다.
MAX_DOCS_PER_REPORT_NUMBER = 30
# 필터에 넣을 raw_ref당 후보 상한
DEFAULT_TOP_K = 3
# CFR 연도판은 base(연도 제거) 그룹당 최신 N건만
DEFAULT_CFR_TOP_N = 1


@dataclass
class Candidate:
    source_id: str
    score: float
    matched_on: str             # 어떤 규칙/컬럼으로 매칭됐는지 (감사용)
    doc_date: str = ""
    section_path: list[str] = field(default_factory=list)


@dataclass
class ResolvedRef:
    raw_citation: str
    kind: str
    candidates: list[Candidate] = field(default_factory=list)
    ambiguous: bool = False     # 한 번호가 너무 많은 문서를 가리켜 특정 불가 → 필터에서 제외


_SECTION_RE = re.compile(r"\d+(?:\.\d+)+|\d+\.\d+|\d+")
_ML_RE = re.compile(r"\bML\d{2}[A-Z0-9]{8}\b", re.IGNORECASE)
_GDC_NUM_RE = re.compile(r"(\d+)")
_CFR_YEAR_RE = re.compile(r"-(?:19|20)\d{2}-")


def _extract_section_number(identifier: str, section_path: list[str] | None = None) -> str:
    """"SRP Section 3.2.2" / "DSRS 10.3" → "3.2.2" / "10.3".

    identifier에 점 구분 섹션 번호가 없으면 section_path에서도 찾는다.
    """
    m = _SECTION_RE.search(identifier or "")
    if m and "." in m.group(0):
        return m.group(0)
    for seg in section_path or []:
        sm = _SECTION_RE.search(str(seg))
        if sm and "." in sm.group(0):
            return sm.group(0)
    return m.group(0) if m else ""


def _section_token_re(sec: str) -> re.Pattern:
    """점 구분 섹션 번호를 완결 토큰으로 매칭(앞뒤가 숫자/점이 아님).

    "10.3" → "Section 10.3,"는 매칭하지만 "10.30"/"110.3"/"10.3.6"은 매칭 안 함.
    """
    return re.compile(r"(?<![\d.])" + re.escape(sec) + r"(?![\d.])")


# FSAR 제목의 챕터 토큰. "FSAR Chapter 15" / "FSAR Ch. 19" 인식.
_CHAPTER_RE = re.compile(r"\b(?:CHAPTER|CH)\.?\s*0*(\d+)", re.IGNORECASE)


def _extract_chapter_number(identifier: str, section_path: list[str] | None = None) -> str:
    """"FSAR Chapter 15"/"Ch. 19" → "15"/"19". 점 섹션("7.1")이면 선두 정수("7")를 챕터로."""
    texts = [identifier or ""] + [str(s) for s in (section_path or [])]
    for t in texts:
        m = _CHAPTER_RE.search(t)
        if m:
            return m.group(1).lstrip("0") or "0"
    for t in texts:  # 폴백: 점 구분 섹션의 선두 정수
        m = re.search(r"\b(\d+)\.\d+", t)
        if m:
            return m.group(1).lstrip("0") or "0"
    return ""


def _chapter_token_re(chap: str) -> re.Pattern:
    """"15" → 제목의 "Chapter 15"/"Chapter 015"는 매칭, "Chapter 150"/"Chapter 1"은 매칭 안 함."""
    return re.compile(r"CHAPTER\s*0*" + re.escape(chap) + r"(?!\d)", re.IGNORECASE)


# RAI 제출번호(3~5자리). 본문 질의번호("03.07.02-6")·2자리 형태는 제출번호로 보지 않는다.
_RAI_SUBNUM_RE = re.compile(r"\bRAI[ \-]?(?:NO\.?\s*)?(\d{3,5})(?![.\-]?\d)", re.IGNORECASE)
# 카탈로그 RAI 행의 제목/CRN에서 제출번호 파싱(최대한 시도, 커버리지 낮음).
_RAI_TITLE_RE = re.compile(r"\bRAI[ \-]?(?:NO\.?\s*)?(\d{3,5})\b", re.IGNORECASE)
_RAI_TITLE_NO_RE = re.compile(
    r"REQUEST FOR ADDITIONAL INFORMATION(?:\s+LETTER)?\s+NO\.?\s*(\d{3,5})", re.IGNORECASE)


def _rai_submission_number(identifier: str) -> str:
    """본문 인용에서 RAI 제출번호 후보(3~5자리)를 뽑는다. 점 구분 질의번호는 제외."""
    m = _RAI_SUBNUM_RE.search(identifier or "")
    if m:
        return m.group(1)
    m2 = re.fullmatch(r"\s*(\d{3,5})\s*", identifier or "")
    return m2.group(1) if m2 else ""


def _cfr_base_key(package_id: str) -> str:
    """CFR-2025-title10-vol1 → CFR-YYYY-title10-vol1 (연도만 다른 동일 vol 묶기)."""
    return _CFR_YEAR_RE.sub("-YYYY-", package_id)


def _canonical_gdc(identifier: str) -> str:
    """"GDC 4" / "Criterion 4" / "General Design Criterion 4" → "GDC4" (정규화 키)."""
    m = _GDC_NUM_RE.search(identifier or "")
    if not m:
        return ""
    return normalize_report_id(f"GDC {m.group(1)}")


def _doctype_contains(row: dict, needle: str) -> bool:
    """DocumentType 리스트(또는 문자열) 안에 needle(부분 일치, 대소문자 무시)이 있는지."""
    dt = row.get("DocumentType")
    if dt is None:
        return False
    items = dt if isinstance(dt, (list, tuple)) else [dt]
    nl = needle.lower()
    return any(nl in str(x).lower() for x in items)


def _dedup_keep_best(candidates: list[Candidate]) -> list[Candidate]:
    """source_id별 최고 점수 1건만 남기고, score desc → doc_date desc로 정렬."""
    best: dict[str, Candidate] = {}
    for c in candidates:
        cur = best.get(c.source_id)
        if cur is None or c.score > cur.score:
            best[c.source_id] = c
    return sorted(best.values(), key=lambda c: (c.score, c.doc_date), reverse=True)


class RefResolver:
    """catalog + 역인덱스를 보유하고 raw 참조를 결정적으로 해소한다."""

    def __init__(
        self,
        catalog: RefCatalog,
        report_index: dict[str, list[IndexEntry]],
        case_index: dict[str, list[IndexEntry]] | None = None,
    ):
        self.catalog = catalog
        self.report_index = report_index
        self.case_index = case_index or {}

        rows = catalog.rows
        # CFR row는 partRange_from/to를 가진 행으로 구조적으로 식별 (DocumentType 상태와 무관).
        self._cfr_rows = [
            r for r in rows
            if str(r.get("partRange_from") or "").strip() != ""
            and str(r.get("packageId") or "").strip() != ""
        ]
        # GDC: 정규화(regulation_number) → subdoc_id
        self._gdc_index: dict[str, dict] = {}
        for r in rows:
            if str(r.get("subdoc_type") or "").strip().upper() == "GDC":
                key = normalize_report_id(str(r.get("regulation_number") or ""))
                if key:
                    self._gdc_index.setdefault(key, r)
        self._srp_rows = [r for r in rows if _doctype_contains(r, "Standard Review Plan")]
        # DSRS 문서는 DocumentType=["NUREG"]로 SRP와 구분이 안 되고 제목으로만 식별 가능
        # (예: "NuScale Design-Specific Review Standard Section 10.3 ...").
        self._dsrs_rows = [
            r for r in rows
            if "design-specific review standard" in str(r.get("DocumentTitle") or "").lower()
        ]
        # FSAR: NuScale FSAR 문서(Tier/Chapter로 분할). 제목 챕터 토큰으로 구분.
        self._fsar_rows = [r for r in rows if _doctype_contains(r, "Final Safety Analysis Report")]
        # RAI: 제출번호 → [문서]. 제목·CaseReferenceNumber에서 최대한 파싱(번호공간 한계로 커버리지 낮음).
        self._rai_index: dict[str, list[IndexEntry]] = {}
        for r in rows:
            if not _doctype_contains(r, "Request for Additional Information"):
                continue
            sid = (r.get("AccessionNumber") or "").strip()
            if not sid:
                continue
            title = str(r.get("DocumentTitle") or "")
            nums = {m.group(1) for m in _RAI_TITLE_RE.finditer(title)}
            nums |= {m.group(1) for m in _RAI_TITLE_NO_RE.finditer(title)}
            crn = r.get("CaseReferenceNumber") or []
            for c in (crn if isinstance(crn, (list, tuple)) else [crn]):
                nums |= {m.group(1) for m in _RAI_TITLE_RE.finditer(str(c))}
            doc_date = str(r.get("DocumentDate") or "")
            for num in nums:
                self._rai_index.setdefault(num, []).append(
                    IndexEntry(source_id=sid, doc_date=doc_date,
                               raw_code=f"RAI {num}", doc_title=title))
        self._valid_ids = catalog.valid_source_ids

    # ------------------------------------------------------------------
    # kind별 해소
    # ------------------------------------------------------------------

    def resolve(self, raw_citation: str, kind: str, identifier: str,
                section_path: list[str] | None = None) -> ResolvedRef:
        kind = (kind or "").strip().upper()
        identifier = (identifier or raw_citation or "").strip()
        section_path = list(section_path or [])
        out = ResolvedRef(raw_citation=raw_citation, kind=kind)

        if kind in ("RG", "NUREG", "TR", "SECY"):
            # 모두 DocumentReportNumber 역인덱스 경로(catalog rows 기반). TR/SECY는 NuScale/etc DRN.
            out.candidates = self._resolve_report_number(identifier, kind, section_path)
        elif kind == "FR":
            out.candidates = self._resolve_fr(identifier)
        elif kind == "CFR":
            out.candidates = self._resolve_cfr(identifier, section_path)
        elif kind == "GDC":
            out.candidates = self._resolve_gdc(identifier)
        elif kind == "SRP":
            out.candidates = self._resolve_section(identifier, self._srp_rows, base_score=0.7,
                                                   section_path=section_path)
        elif kind == "DSRS":
            out.candidates = self._resolve_section(identifier, self._dsrs_rows, base_score=0.6,
                                                   section_path=section_path)
        elif kind == "FSAR":
            out.candidates = self._resolve_fsar(identifier, section_path)
        elif kind == "RAI":
            out.candidates = self._resolve_rai(identifier, section_path)
        elif kind == "ML":
            out.candidates = self._resolve_ml(identifier)
        else:  # OTHER 등 카탈로그 밖
            out.candidates = []

        # 섹션으로도 못 좁힌 채 후보가 여전히 너무 많으면(= 번호 하나가 다수 문서를 가리킴) 특정 불가 → 필터 제외
        out.ambiguous = len(out.candidates) > MAX_DOCS_PER_REPORT_NUMBER
        return out

    def _resolve_report_number(self, identifier: str, kind: str,
                               section_path: list[str] | None = None) -> list[Candidate]:
        key = normalize_report_id(identifier)
        cands: list[Candidate] = []
        if key:
            entries = self.report_index.get(key, [])
            # 한 번호가 다수 문서를 공유하는 경우(예: NUREG-0800은 949개 SRP 섹션이 같은 번호)
            # → 섹션 번호로 DocumentTitle을 매칭해 좁힌다.
            sec = _extract_section_number(identifier, section_path)
            if len(entries) > MAX_DOCS_PER_REPORT_NUMBER and sec:
                token = _section_token_re(sec)
                narrowed = [e for e in entries if token.search(e.doc_title or "")]
                if narrowed:
                    entries = narrowed
                    for e in entries:
                        cands.append(Candidate(
                            source_id=e.source_id, score=0.9, doc_date=e.doc_date,
                            matched_on=f"DocumentReportNumber={e.raw_code}+Title§{sec}",
                            section_path=[f"Section {sec}"],
                        ))
            if not cands:
                for e in entries:
                    cands.append(Candidate(
                        source_id=e.source_id, score=0.9, doc_date=e.doc_date,
                        matched_on=f"DocumentReportNumber={e.raw_code}",
                    ))
            # 1차 미스 → CaseReferenceNumber 보조 (낮은 신뢰도: 그 문서를 다루는 FR notice 등)
            if not cands:
                for e in self.case_index.get(key, []):
                    cands.append(Candidate(
                        source_id=e.source_id, score=0.4, doc_date=e.doc_date,
                        matched_on=f"CaseReferenceNumber={e.raw_code}",
                    ))
        return _dedup_keep_best(cands)

    def _resolve_fr(self, identifier: str) -> list[Candidate]:
        key = normalize_report_id(identifier)
        cands: list[Candidate] = []
        if key:
            for e in self.case_index.get(key, []):
                cands.append(Candidate(
                    source_id=e.source_id, score=0.85, doc_date=e.doc_date,
                    matched_on=f"CaseReferenceNumber={e.raw_code}",
                ))
        return _dedup_keep_best(cands)

    def _resolve_cfr(self, identifier: str, section_path: list[str]) -> list[Candidate]:
        part = parse_cfr_part(identifier)
        if part is None:
            return []
        sp = section_path or [f"Part {part}"]
        cands: list[Candidate] = []
        for r in self._cfr_rows:
            if part_in_range(part, r.get("partRange_from"), r.get("partRange_to")):
                pid = str(r.get("packageId") or "").strip()
                if not pid:
                    continue
                cands.append(Candidate(
                    source_id=pid, score=0.8,
                    doc_date=str(r.get("dateIssued") or ""),
                    matched_on=f"partRange[{r.get('partRange_from')}-{r.get('partRange_to')}]∋{part}",
                    section_path=list(sp),
                ))
        # 연도판 접기: base(연도 제거) 그룹별 최신 dateIssued만 남긴다.
        return self._collapse_cfr_editions(cands)

    @staticmethod
    def _collapse_cfr_editions(cands: list[Candidate], top_n: int = DEFAULT_CFR_TOP_N) -> list[Candidate]:
        groups: dict[str, list[Candidate]] = {}
        for c in cands:
            groups.setdefault(_cfr_base_key(c.source_id), []).append(c)
        kept: list[Candidate] = []
        for grp in groups.values():
            grp.sort(key=lambda c: c.doc_date, reverse=True)  # 최신 연도 우선
            kept.extend(grp[:top_n])
        return _dedup_keep_best(kept)

    def _resolve_gdc(self, identifier: str) -> list[Candidate]:
        key = _canonical_gdc(identifier)
        row = self._gdc_index.get(key)
        if not row:
            return []
        sid = str(row.get("subdoc_id") or "").strip()
        if not sid:
            return []
        return [Candidate(
            source_id=sid, score=0.9,
            matched_on=f"regulation_number={row.get('regulation_number')}",
        )]

    def _resolve_section(self, identifier: str, rows: list[dict], base_score: float,
                         section_path: list[str] | None = None) -> list[Candidate]:
        sec = _extract_section_number(identifier, section_path)
        sp = [f"Section {sec}"] if sec else []
        cands: list[Candidate] = []
        token = _section_token_re(sec) if sec else None
        for r in rows:
            sid = str(r.get("AccessionNumber") or "").strip()
            if not sid:
                continue
            title = str(r.get("DocumentTitle") or "")
            # substring이 아니라 완결 토큰 매칭 ("3.2.2"가 "13.2.2"를 잡지 않게)
            if token and token.search(title):
                cands.append(Candidate(
                    source_id=sid, score=base_score,
                    doc_date=str(r.get("DocumentDate") or ""),
                    matched_on=f"DocumentTitle§{sec}", section_path=list(sp),
                ))
        # 섹션번호 정확 매칭이 없으면 계열 문서로 약하게 폴백(점수 절반)
        if not cands and sec:
            for r in rows:
                sid = str(r.get("AccessionNumber") or "").strip()
                if sid:
                    cands.append(Candidate(
                        source_id=sid, score=round(base_score / 2, 3),
                        doc_date=str(r.get("DocumentDate") or ""),
                        matched_on="series-fallback", section_path=list(sp),
                    ))
        return _dedup_keep_best(cands)

    def _resolve_fsar(self, identifier: str, section_path: list[str]) -> list[Candidate]:
        """NuScale FSAR → 제목의 챕터 토큰으로 해당 챕터 문서를 좁힌다.

        챕터 단서가 없는 "FSAR" 단독은 157개 분할 문서 전부를 가리켜 너무 막연하므로
        후보를 내지 않는다(필터 미적용). 챕터 매칭 시 score 0.7.
        """
        chap = _extract_chapter_number(identifier, section_path)
        if not chap:
            return []
        token = _chapter_token_re(chap)
        sp = [f"Chapter {chap}"]
        cands: list[Candidate] = []
        for r in self._fsar_rows:
            sid = (r.get("AccessionNumber") or "").strip()
            if not sid:
                continue
            if token.search(str(r.get("DocumentTitle") or "")):
                cands.append(Candidate(
                    source_id=sid, score=0.7, doc_date=str(r.get("DocumentDate") or ""),
                    matched_on=f"FSAR DocumentTitle§Chapter {chap}", section_path=list(sp)))
        return _dedup_keep_best(cands)

    def _resolve_rai(self, identifier: str, section_path: list[str]) -> list[Candidate]:
        """NuScale RAI → 제출번호(3~5자리)로 매칭(best-effort).

        본문의 RAI 질의번호(예: 03.07.02-6)는 카탈로그 RAI 문서의 제출번호와 번호공간이
        달라 매핑할 수 없으므로 매칭하지 않는다(section_path에만 보존). 제출번호가 제목/CRN에
        파싱된 소수의 RAI만 매칭되며 점수 0.6.
        """
        num = _rai_submission_number(identifier)
        if not num:
            return []
        cands: list[Candidate] = []
        for e in self._rai_index.get(num, []):
            cands.append(Candidate(
                source_id=e.source_id, score=0.6, doc_date=e.doc_date,
                matched_on=f"RAI#{num}(title/CRN)", section_path=list(section_path or [])))
        return _dedup_keep_best(cands)

    def _resolve_ml(self, identifier: str) -> list[Candidate]:
        m = _ML_RE.search(identifier)
        sid = m.group(0).upper() if m else identifier.strip().upper()
        if sid and sid in self._valid_ids:
            return [Candidate(source_id=sid, score=1.0, matched_on="AccessionNumber")]
        return []

    # ------------------------------------------------------------------
    # 일괄 처리 + 필터 생성
    # ------------------------------------------------------------------

    def resolve_many(self, raw_refs: list) -> list[ResolvedRef]:
        """raw_refs: RawRef dataclass 또는 dict(raw_citation/kind/identifier/section_path) 리스트."""
        out: list[ResolvedRef] = []
        for ref in raw_refs:
            if isinstance(ref, dict):
                out.append(self.resolve(
                    ref.get("raw_citation", ""), ref.get("kind", ""),
                    ref.get("identifier", ""), ref.get("section_path"),
                ))
            else:  # RawRef
                out.append(self.resolve(
                    getattr(ref, "raw_citation", ""), getattr(ref, "kind", ""),
                    getattr(ref, "identifier", ""), getattr(ref, "section_path", None),
                ))
        return out


def build_source_id_filter(
    resolved: list[ResolvedRef], *, min_score: float = 0.6, top_k: int = DEFAULT_TOP_K
) -> dict | None:
    """질의 시점 OpenSearch 필터 절. 임계값 이상 후보의 source_id 합집합을 terms로.

    - 기본 임계값 0.6은 DSRS(0.6)까지 포함하고 CaseReferenceNumber 폴백(0.4)은 제외.
    - raw_ref당 (score, 최신성)으로 랭킹된 후보 중 상위 ``top_k``만 채택(과매핑 방지).
    - ``ambiguous`` raw_ref(번호 하나가 다수 문서를 가리켜 특정 불가)는 통째로 제외.
    매칭이 하나도 없으면 None(필터 미적용).
    """
    ids: list[str] = []
    seen: set[str] = set()
    for r in resolved:
        if r.ambiguous:
            continue
        taken = 0
        for c in r.candidates:  # 이미 (score, doc_date) desc로 정렬됨
            if taken >= top_k:
                break
            if c.score >= min_score:
                taken += 1
                if c.source_id not in seen:
                    seen.add(c.source_id)
                    ids.append(c.source_id)
    if not ids:
        return None
    return {"terms": {"source_id": ids}}
