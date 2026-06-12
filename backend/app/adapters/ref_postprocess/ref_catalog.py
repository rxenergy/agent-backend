"""NRC_MANUAL CSV + NuScale JSON 통합 catalog.

원본 컬럼 이름(``AccessionNumber``, ``DocumentType`` 등)을 그대로 유지한다.

소스 종류별 키 세트:
- **ADAMS row** (NRC_MANUAL의 AccessionNumber 행 + NuScale): ``AccessionNumber``,
  ``DocumentTitle``, ``DocumentType``, ``DocumentReportNumber``, ``DocumentDate``,
  ``DocketNumber``, ``LicenseNumber``, ``AuthorName``, ``AuthorAffiliation``,
  ``AddresseeName``, ``AddresseeAffiliation``, ``CaseReferenceNumber``,
  ``Keyword``, ``Comment``, ``IsPackage``, ``PackageNumber``, ``Url`` 등.
- **CFR row** (NRC_MANUAL의 packageId 행): ``packageId``, ``title``,
  ``documentType``, ``dateIssued``, ``partRange_from``, ``partRange_to``,
  ``governmentAuthor1``, ``governmentAuthor2``, ``publisher``,
  ``collectionCode``, ``_path``.

파생 컬럼 (모든 row에 항상 존재):
- ``source_type`` ∈ {NRC_MANUAL_ADAMS, NRC_MANUAL_CFR, NuScale}
- ``doc_category`` (NRC_MANUAL: _path 첫 segment, NuScale: 부모 폴더명)

ref_source_id 자동 선택:
- AccessionNumber가 있으면 그것, 없으면 packageId.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Identifier normalization (RG/NUREG/CFR 표기 동치 처리)
# ---------------------------------------------------------------------------

_NORMALIZE_RE = re.compile(r"[\s\-_/]+")
# ", Rev 6", ", Rev. 4", ", Revision 0", "REV2" 등 revision 접미사 제거 (구분자 제거 후 적용)
_REV_TAIL_RE = re.compile(r",?REV(?:ISION)?\.?\d+.*$")
# 서술 prefix 제거: "(NuScale) Topical/Technical Report TR-..." → "TR-..."
_REPORT_PREFIX_RE = re.compile(r"^(?:NUSCALE)?(?:TOPICAL|TECHNICAL)REPORT")
# RG의 소수부 leading zero 제거. NUREG의 fixed-width 코드(0800 등)는 보존하기 위해 RG만 처리.
_RG_PAD_RE = re.compile(r"^(RG)(\d+)\.0*(\d+)")
# NuScale TR 패밀리(TR/NP-TR/NP-DEM/NP-PL/NP-RP) 식별 — 이 경우에만 NP prefix·독점 접미사 정리.
_TR_FAMILY_RE = re.compile(r"^(?:NP)?(?:TR|DEM|PL|RP)")
# 선두 NuScale 사내 prefix "NP" 제거 (뒤가 TR/DEM/PL/RP일 때만). NP-TR-... ≡ TR-...
_TR_LEAD_NP_RE = re.compile(r"^NP(?=(?:TR|DEM|PL|RP))")
# 말미 독점/승인 접미사 제거: -P / -NP / -A / -NP-A → 동일 TR로 접힘.
_TR_SUFFIX_RE = re.compile(r"(?:NPA|NP|P|A)$")


def _canonical_tr(s: str) -> str:
    """TR 패밀리 코드의 NP prefix·독점 접미사를 제거해 표기 변형을 한 키로 접는다.

    "NP-TR-1010-859-NP" / "TR-1010-859" → "TR1010859"
    "TR-0515-13952-NP-A" → "TR051513952"
    단 6자리 일련 "TR-102621"(→TR102621)과 MMYY형 "TR-0610-289"(→TR0610289)는
    구조가 달라 서로 다른 키로 유지된다(동치화하지 않음).
    """
    s = _TR_LEAD_NP_RE.sub("", s)
    s = _TR_SUFFIX_RE.sub("", s)
    return s


def normalize_report_id(raw: str) -> str:
    """대소문자 통일 + 구분자 제거 + RG zero-padding 제거 + Rev/접미사 제거.

    동치 처리:
      - "RG 1.29" / "RG-1.29" / "RG-1.029" / "RG-1.029, Rev 6" /
        "Regulatory Guide 1.29" → 모두 "RG1.29"
      - "NUREG-0800" → "NUREG0800" (fixed-width, padding 유지)
      - "10 CFR Part 50" → "10CFRPART50" (CFR의 "50.55a" 끝 A는 보존)
      - "NP-TR-1010-859-NP" / "TR-1010-859" → "TR1010859"
        "Topical Report TR-0516-49416, Revision 0" → "TR051649416"
    """
    if not raw:
        return ""
    s = raw.strip().upper()
    s = s.replace("REGULATORY GUIDE", "RG")
    s = _NORMALIZE_RE.sub("", s)
    s = _REPORT_PREFIX_RE.sub("", s)
    s = _REV_TAIL_RE.sub("", s)
    s = _RG_PAD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}.{m.group(3)}", s)
    # TR 패밀리에 한해 NP prefix·독점 접미사 정리 (RG/NUREG/CFR 키는 건드리지 않음)
    if _TR_FAMILY_RE.match(s):
        s = _canonical_tr(s)
    return s


# 정규화 매칭(케이스/하이픈/공백/RG zero-padding/Rev 접미사 무시)
_NORMALIZED_COLUMNS = frozenset({
    "AccessionNumber",
    "packageId",
    "DocumentReportNumber",
    "subdoc_id",
    "regulation_number",
})
# 날짜 prefix 매칭 (YYYY-MM-DD)
_DATE_COLUMNS = frozenset({
    "DocumentDate",
    "DateAdded",
    "DateAddedTimestamp",
    "DateDocketed",
    "dateIssued",
    "lastModified",
})

# 모든 row가 가지는 파생 컬럼
DERIVED_COLUMNS: tuple[str, ...] = ("source_type", "doc_category")

# ADAMS 키 (NRC_MANUAL의 AccessionNumber 행 + NuScale 공통)
ADAMS_COLUMNS: tuple[str, ...] = (
    "AccessionNumber",
    "DocumentTitle",
    "DocumentDate",
    "DocumentType",
    "DocumentReportNumber",
    "DocketNumber",
    "LicenseNumber",
    "AuthorName",
    "AuthorAffiliation",
    "AddresseeName",
    "AddresseeAffiliation",
    "ContactPerson",
    "CaseReferenceNumber",
    "Keyword",
    "Comment",
    "PackageNumber",
    "PackagesFiledIn",
    "DocumentsFiledInPackage",
    "EstimatedPageCount",
    "DateAdded",
    "DateAddedTimestamp",
    "DateDocketed",
    "IsPackage",
    "IsLegacy",
    "Availability",
    "ItemType",
    "DistributionListCodes",
    "MicroformAddresses",
    "Url",
)

# CFR 전용 키 (NRC_MANUAL의 packageId 행)
CFR_COLUMNS: tuple[str, ...] = (
    "packageId",
    "title",
    "documentType",
    "dateIssued",
    "partRange_from",
    "partRange_to",
    "governmentAuthor1",
    "governmentAuthor2",
    "publisher",
    "collectionCode",
    "collectionName",
    "category",
    "branch",
    "_path",
    "download_pdfLink",
    "detailsLink",
    "note",
)

# 모든 컬럼 union (LLM에 노출되는 query 가능 컬럼)
# SUBDOC 컬럼군 (sub-document row 전용)
SUBDOC_COLUMNS: tuple[str, ...] = (
    "subdoc_id",
    "subdoc_type",
    "parent_source_id",
    "regulation_number",
    "subdoc_name",
    "anchor_chunk_id",
)

CATALOG_COLUMNS: tuple[str, ...] = DERIVED_COLUMNS + ADAMS_COLUMNS + CFR_COLUMNS + SUBDOC_COLUMNS


@dataclass(frozen=True)
class RefCatalog:
    rows: tuple[dict, ...]
    valid_source_ids: frozenset[str]
    columns: tuple[str, ...]
    mtime: float

    def query(
        self,
        filters: dict[str, str],
        columns_to_return: Sequence[str] = ("AccessionNumber", "packageId", "DocumentTitle", "title", "doc_category"),
        top_k: int = 20,
    ) -> list[dict]:
        if not filters:
            return []
        active_filters = [
            (col, q) for col, q in filters.items()
            if col in self.columns and (q or "").strip()
        ]
        if not active_filters:
            return []

        wanted = tuple(c for c in columns_to_return if c in self.columns) or ("AccessionNumber",)
        out: list[dict] = []
        for row in self.rows:
            if all(_match(col, q, row.get(col)) for col, q in active_filters):
                # 비어있는 컬럼은 결과에서 제외하여 토큰 절약
                projection = {c: row[c] for c in wanted if c in row and _truthy(row[c])}
                out.append(projection)
                if len(out) >= top_k:
                    break
        return out

    def to_json(self) -> dict:
        return {
            "mtime": self.mtime,
            "row_count": len(self.rows),
            "columns": list(self.columns),
            "valid_source_ids": sorted(self.valid_source_ids),
            "rows": list(self.rows),
        }

    @classmethod
    def from_json(cls, data: dict) -> "RefCatalog":
        return cls(
            rows=tuple(data.get("rows", [])),
            valid_source_ids=frozenset(data.get("valid_source_ids", [])),
            columns=tuple(data.get("columns", CATALOG_COLUMNS)),
            mtime=float(data.get("mtime", 0.0)),
        )


def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    return str(v).strip() != ""


def _match(column: str, query: str, value) -> bool:
    q = (query or "").strip()
    if not q:
        return True
    ql = q.lower()

    if isinstance(value, list):
        return any(_match(column, q, v) for v in value)
    if value is None:
        return False
    v = str(value)

    if column in _NORMALIZED_COLUMNS:
        nq = normalize_report_id(q)
        if not nq:
            return False
        return nq in normalize_report_id(v)
    if column in _DATE_COLUMNS:
        return v.lower().startswith(ql)
    return ql in v.lower()


def source_id_for_row(row: dict) -> str:
    """row의 식별자.

    우선순위: subdoc_id (SUBDOC row) → AccessionNumber (ADAMS) → packageId (CFR).
    """
    sd = row.get("subdoc_id")
    if isinstance(sd, str):
        sd_stripped = sd.strip()
        if sd_stripped:
            return sd_stripped
    accession = (row.get("AccessionNumber") or "").strip() if isinstance(row.get("AccessionNumber"), str) else ""
    if accession:
        return accession
    package = (row.get("packageId") or "").strip() if isinstance(row.get("packageId"), str) else ""
    return package


# ---------------------------------------------------------------------------
# 빌더
# ---------------------------------------------------------------------------


def _normalize_list(value) -> list[str]:
    """CSV의 stringified list 또는 JSON list를 list[str]로."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    s = str(value).strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            import ast

            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, SyntaxError):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except (ValueError, json.JSONDecodeError):
                pass
    return [s]


def _first_path_segment(path: str) -> str:
    p = (path or "").strip().lstrip("/").lstrip("./")
    if not p:
        return ""
    return p.split("/", 1)[0]


# ADAMS row에서 list 형태로 normalize할 키들
_ADAMS_LIST_FIELDS = frozenset({
    "DocumentType",
    "DocumentReportNumber",
    "DocketNumber",
    "LicenseNumber",
    "AuthorName",
    "AuthorAffiliation",
    "AddresseeName",
    "AddresseeAffiliation",
    "CaseReferenceNumber",
    "Keyword",
    "PackagesFiledIn",
    "DocumentsFiledInPackage",
    "DistributionListCodes",
    "MicroformAddresses",
})


def _row_from_nrc_csv(raw: dict) -> dict | None:
    """NRC_MANUAL metadata.csv 한 row를 원본 키 구조로 변환."""
    accession = (raw.get("AccessionNumber") or "").strip()
    package_id = (raw.get("packageId") or "").strip()
    if not (accession or package_id):
        return None

    path = (raw.get("_path") or "").strip()
    doc_category = _first_path_segment(path)

    if accession:
        # ADAMS row
        row: dict = {
            "source_type": "NRC_MANUAL_ADAMS",
            "doc_category": doc_category,
        }
        for col in ADAMS_COLUMNS:
            v = raw.get(col)
            if col in _ADAMS_LIST_FIELDS:
                row[col] = _normalize_list(v)
            else:
                row[col] = (v or "").strip() if isinstance(v, str) else (v or "")
        return row

    # CFR row
    row = {
        "source_type": "NRC_MANUAL_CFR",
        "doc_category": doc_category,
    }
    for col in CFR_COLUMNS:
        v = raw.get(col)
        row[col] = (v or "").strip() if isinstance(v, str) else (v or "")
    # metadata.csv가 camelCase ``documentType`` 컬럼을 삭제하고 리스트값
    # ``DocumentType``(예: ["CFR"], ["FR"])으로 통일되었으므로 CFR row에도 함께 싣는다.
    row["DocumentType"] = _normalize_list(raw.get("DocumentType"))
    return row



def _row_from_subdoc_jsonl(raw: dict) -> dict | None:
    """JSONL의 SUBDOC entry를 catalog row 형식으로 변환.

    SUBDOC 컬럼만 채우고 ADAMS/CFR 전용 컬럼은 빈 값. source_type이 명시되어
    있어야 하며 subdoc_id가 없으면 None.
    """
    if not isinstance(raw, dict):
        return None
    sd = (raw.get("subdoc_id") or "").strip() if isinstance(raw.get("subdoc_id"), str) else ""
    if not sd:
        return None
    row: dict = {
        "source_type": raw.get("source_type") or "NRC_MANUAL_SUBDOC",
        "doc_category": (raw.get("doc_category") or "").strip()
        if isinstance(raw.get("doc_category"), str) else "",
    }
    for col in SUBDOC_COLUMNS:
        v = raw.get(col)
        row[col] = (v or "").strip() if isinstance(v, str) else (v or "")
    return row


def _iter_subdoc_rows(subdoc_path: Path) -> Iterable[dict]:
    """JSONL 파일을 한 줄씩 읽어 SUBDOC row를 yield."""
    with subdoc_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = _row_from_subdoc_jsonl(raw)
            if row is not None:
                yield row


def build_catalog(
    csv_path: Path,
    subdoc_path: Path | None = None,
) -> RefCatalog:
    rows: list[dict] = []
    seen: set[str] = set()
    mtime = csv_path.stat().st_mtime

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = _row_from_nrc_csv(raw)
            if row is None:
                continue
            sid = source_id_for_row(row)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            rows.append(row)

    if subdoc_path is not None and subdoc_path.exists():
        sd_mtime = subdoc_path.stat().st_mtime
        if sd_mtime > mtime:
            mtime = sd_mtime
        for row in _iter_subdoc_rows(subdoc_path):
            sid = source_id_for_row(row)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            rows.append(row)

    return RefCatalog(
        rows=tuple(rows),
        valid_source_ids=frozenset(seen),
        columns=CATALOG_COLUMNS,
        mtime=mtime,
    )


def load_or_build_catalog(
    csv_path: Path,
    cache_path: Path,
    subdoc_path: Path | None = None,
) -> RefCatalog:
    csv_mtime = csv_path.stat().st_mtime
    sd_mtime = subdoc_path.stat().st_mtime if subdoc_path and subdoc_path.exists() else 0.0
    expected_mtime = max(csv_mtime, sd_mtime)

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if abs(float(cached.get("mtime", 0.0)) - expected_mtime) < 1e-6:
                return RefCatalog.from_json(cached)
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    catalog = build_catalog(csv_path, subdoc_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(catalog.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(cache_path)
    return catalog


# ---------------------------------------------------------------------------
# Rule-base 해소용 보조 인덱스 / 헬퍼 (LLM 없는 결정적 매핑에서 사용)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    """역인덱스 1개 엔트리: report number/case reference → 문서 식별자."""

    source_id: str       # AccessionNumber
    doc_date: str        # DocumentDate (랭킹/최신판 선택용, 빈 문자열 허용)
    raw_code: str        # 원본 코드 (예: "RG-1.068", "81FR88719")
    doc_title: str = ""  # DocumentTitle (한 번호가 다수 문서를 공유할 때 섹션으로 구분, 예: "... Section 10.3, Revision 4")


def _build_id_index(
    csv_path: Path, value_column: str
) -> dict[str, list[IndexEntry]]:
    """metadata CSV의 list-valued 식별자 컬럼을 정규화 코드 → [IndexEntry...]로.

    한 행(AccessionNumber)이 여러 코드를 묶을 수 있고, 한 코드가 여러 행에
    나타날 수도 있으므로 값은 리스트. 정규화는 ``normalize_report_id``로
    통일하여 "RG 1.68"/"RG-1.068"/"RG1.68" 등이 같은 키로 모인다.
    """
    index: dict[str, list[IndexEntry]] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            accession = (raw.get("AccessionNumber") or "").strip()
            if not accession:
                continue
            doc_date = (raw.get("DocumentDate") or "").strip()
            doc_title = (raw.get("DocumentTitle") or "").strip()
            for code in _normalize_list(raw.get(value_column)):
                key = normalize_report_id(code)
                if not key:
                    continue
                index.setdefault(key, []).append(
                    IndexEntry(source_id=accession, doc_date=doc_date,
                               raw_code=code, doc_title=doc_title)
                )
    return index


def build_report_number_index(csv_path: Path) -> dict[str, list[IndexEntry]]:
    """``DocumentReportNumber`` 역인덱스 (legacy, CSV 전용).

    .. deprecated::
        ``csv_path``(NRC_MANUAL metadata.csv)만 읽어 **NuScale/subdoc 행이 빠진다**.
        권장 경로는 :func:`build_report_number_index_from_catalog` — 이미 빌드한
        catalog의 rows(NuScale 포함)로 인덱스를 만들어 카탈로그와 일관성을 보장한다.
        이 함수는 카탈로그 없이 CSV만으로 빠르게 인덱스가 필요한 경우에만 쓴다.

    **전체 metadata.csv를 입력으로 사용할 것.** 빈 행은 자동으로 건너뛴다(2793행은
    충분히 작아 사전 필터가 불필요). 부분 추출본(예: metadata_ReportNumber.csv)은
    실제 RG 문서 다수가 누락되어 있어(RG-1.068 등) 매핑 누락을 유발하므로 권장하지 않는다.
    """
    return _build_id_index(csv_path, "DocumentReportNumber")


def build_case_reference_index(csv_path: Path) -> dict[str, list[IndexEntry]]:
    """``CaseReferenceNumber`` 보조 역인덱스 (legacy, CSV 전용).

    .. deprecated::
        :func:`build_report_number_index`와 동일 사유로
        :func:`build_case_reference_index_from_catalog` 사용을 권장한다.

    FR 인용/NRC 도켓/RG 코드 등이 섞여 있다.
    """
    return _build_id_index(csv_path, "CaseReferenceNumber")


def _build_id_index_from_rows(
    rows: Iterable[dict], value_column: str
) -> dict[str, list[IndexEntry]]:
    """catalog rows에서 list-valued 식별자 컬럼을 정규화 코드 → [IndexEntry...]로.

    :func:`_build_id_index`(CSV 전용)의 row 이터러블 버전. source_id는
    :func:`source_id_for_row`(AccessionNumber/packageId/subdoc_id 통합)로 뽑아
    카탈로그와 일관성을 유지한다. catalog rows의 list 컬럼은 이미 정규화돼 있어
    ``_normalize_list``를 멱등 통과한다.
    """
    index: dict[str, list[IndexEntry]] = {}
    for row in rows:
        sid = source_id_for_row(row)
        if not sid:
            continue
        doc_date = str(row.get("DocumentDate") or "").strip()
        doc_title = str(row.get("DocumentTitle") or "").strip()
        for code in _normalize_list(row.get(value_column)):
            key = normalize_report_id(code)
            if not key:
                continue
            index.setdefault(key, []).append(
                IndexEntry(source_id=sid, doc_date=doc_date,
                           raw_code=code, doc_title=doc_title)
            )
    return index


def build_report_number_index_from_catalog(catalog: RefCatalog) -> dict[str, list[IndexEntry]]:
    """``DocumentReportNumber`` 역인덱스 (권장, catalog rows 기반).

    ``catalog.rows``는 NRC_MANUAL + NuScale + subdoc 행을 모두 포함하므로 NuScale의
    DocumentReportNumber(예: ``NP-TR-1010-859-NP``)도 자동 편입된다.
    """
    return _build_id_index_from_rows(catalog.rows, "DocumentReportNumber")


def build_case_reference_index_from_catalog(catalog: RefCatalog) -> dict[str, list[IndexEntry]]:
    """``CaseReferenceNumber`` 보조 역인덱스 (권장, catalog rows 기반)."""
    return _build_id_index_from_rows(catalog.rows, "CaseReferenceNumber")


_CFR_PART_RE = re.compile(r"(?:PART\s*)?(\d+)", re.IGNORECASE)


def parse_cfr_part(identifier: str) -> int | None:
    """CFR 인용에서 Part 정수를 추출.

    "10 CFR 50.55a" → 50, "10 CFR Part 50" → 50, "50.55" → 50, "Part 100" → 100.
    title(10/CFR) 토큰은 건너뛰고 첫 Part 번호를 찾는다.
    """
    if not identifier:
        return None
    s = identifier.upper()
    # title 표기("10 CFR", "TITLE 10") 제거 후 첫 정수를 Part로 본다.
    s = re.sub(r"\b10\s*CFR\b", " ", s)
    s = re.sub(r"\bTITLE\s*10\b", " ", s)
    s = s.replace("CFR", " ")
    m = _CFR_PART_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def part_in_range(part: int, from_, to_) -> bool:
    """part가 [from_, to_] 폐구간에 포함되는지. 범위 값이 비거나 비정수면 False."""
    try:
        lo = int(str(from_).strip())
        hi = int(str(to_).strip())
    except (TypeError, ValueError):
        return False
    return lo <= part <= hi
