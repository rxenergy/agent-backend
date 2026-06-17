#!/usr/bin/env python3
"""10 CFR Part→Page 스코프 맵 빌더 (오프라인, 1회+연차판 갱신 시).

설계: docs/plans/spec_driven_10cfr_part_page_map.design.v1.md §3.2.

문제: 10CFR `std_canonical_id` 는 govinfo 연차판 *볼륨* 단위(`10CFR-Part1-50` 등
~1,000페이지 묶음)라 Part 단위 스코프가 불가능하다. chunk 의 `part_no` 는 0.3%만
채워져 있어(실측) 쓸 수 없다. 그러나 chunk 본문(text)에 govinfo XML 구조 헤더
(`PART {N}—{TITLE}`)가 보존돼 있어, 볼륨을 페이지순으로 훑어 Part 시작 페이지를
추출할 수 있다.

본 스크립트는 nrc-all-v1 인덱스에서 Title 10 vol1/vol2(NRC Chapter I = Parts
1–199) 의 chunk 를 페이지순으로 끌어와 §2 의 원자력 관련 Part 각각의 (vol_base,
page_start, page_end) 를 산출해 정적 JSON 으로 출력한다. 런타임 N2 는 이 JSON 만
로드한다(검색 경로에 추출 비용 없음).

추출 단계(§3.2):
  1. 대상 packageId 열거 — CFR-*-title10-vol{1,2} 만.
  2. 각 packageId chunk 를 page_start asc 전수 조회(_source=[page_start, page_end, text]).
  3. 헤더 정규식(`^PART {N}—{TITLE}`, em/en-dash/hyphen, 대문자) 으로 Part 시작 페이지.
  4. 누락 보강 — 헤더가 chunk 경계에 걸려 놓친 Part 는 Authority:/Source: 근접으로
     후보 추정. §2 Tier1/2 Part 가 전부 잡힐 때까지(완전성 게이트 — 누락은 log 명시).
  5. Part 정렬 → page_end = 다음 Part start − 1. 여러 연차판의 동일 Part 시작 페이지
     일치를 검증(편차 시 최신판 기준). JSON + 빌드 로그 출력.

Part 53 은 적재 코퍼스에서 `[RESERVED]`(본문 없음) — page 미산출, reserved=true 로
명시 기록(코퍼스에 본문 적재 시 재실행으로 충전).

사용:
  python3 scripts/build_10cfr_part_page_map.py \
    --endpoint http://localhost:9200 --index nrc-all-v1 \
    --out backend/app/application/intake/_10cfr_part_pages.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import defaultdict
from typing import Any

# === §2 원자력 관련 Part 목록 (eCFR/govinfo ground-truth) ===========================
# Tier1 = 원자로 인허가/안전 직접, Tier2 = 연료주기/물질/수송/회계. vol 배정은 Part
# 번호 기준 추정 — 빌드가 packageId 별 헤더 실측으로 확정(여기 값은 완전성 게이트의
# 기대 목록일 뿐). 제목은 eCFR versioner API verbatim.
NUCLEAR_PARTS: dict[int, str] = {
    2: "Agency Rules of Practice and Procedure",
    19: "Notices, Instructions and Reports to Workers: Inspection and Investigations",
    20: "Standards for Protection Against Radiation",
    21: "Reporting of Defects and Noncompliance",
    26: "Fitness for Duty Programs",
    40: "Domestic Licensing of Source Material",
    50: "Domestic Licensing of Production and Utilization Facilities",
    51: "Environmental Protection Regulations for Domestic Licensing and Related Regulatory Functions",
    52: "Licenses, Certifications, and Approvals for Nuclear Power Plants",
    53: "Risk-Informed, Technology-Inclusive Regulatory Framework for Commercial Nuclear Plants",
    54: "Requirements for Renewal of Operating Licenses for Nuclear Power Plants",
    55: "Operators' Licenses",
    70: "Domestic Licensing of Special Nuclear Material",
    71: "Packaging and Transportation of Radioactive Material",
    72: "Licensing Requirements for the Independent Storage of Spent Nuclear Fuel, HLW, and Reactor-Related GTCC Waste",
    73: "Physical Protection of Plants and Materials",
    74: "Material Control and Accounting of Special Nuclear Material",
    100: "Reactor Site Criteria",
    140: "Financial Protection Requirements and Indemnity Agreements",
}

# Part body 시작 페이지 추출은 두 신호를 결합한다(단일 신호로는 청크 분할·교차참조 노이즈
# 때문에 신뢰 불가 — 실측 확인). vol 내 Part 시작 페이지는 *단조증가* 해야 한다는 제약을
# 둘 다에 강제한다.
#
# (A) 헤더 앵커(trusted) — `^PART {N}—{XX...}`(em/en/hyphen, 대문자 2자 이상). 줄 시작·전부
#     대문자 본문 헤더만 매칭(교차참조 "see Part 50"·TOC 라인 배제). 살아남는 곳은 정확.
# (B) 섹션 밀도 백스톱 — 헤더가 청크 경계에 잘려 없는 Part 는, `§{N}.{M}` body-section 이
#     *그 페이지에서 지배적*(해당 Part 섹션 카운트가 최다·≥2)으로 처음 등장하는 페이지로
#     추정. 인접 헤더 앵커 사이 구간에 떨어질 때만 채택(단조성 보존).
_HEADER_RE = re.compile(r"(?m)^[ \t]*PART\s+(\d+)([A-Z]?)\s*[—–-]\s*([A-Z][A-Z][^\n]{0,90})")
_RESERVED_RE = re.compile(r"(?m)^[ \t]*PART\s+(\d+)([A-Z]?)\s*\[RESERVED\]", re.IGNORECASE)
_SECTION_RE = re.compile(r"§\s*(\d+)\.\d+")

# 연차판은 최신 N개만 본다(오래된 판은 청크/OCR 표기가 흔들려 페이지 편차 노이즈를 만든다 —
# 실측: 2024·2025 는 헤더가 동일 페이지, 그 이전은 매칭 불안정). base 별 최신 2판 합의.
_NEWEST_EDITIONS = 2

_PKG_RE = re.compile(r"^CFR-(\d{4})-(title10-vol[12])$")

# 볼륨 Part 범위(govinfo + 인덱스 partRange 실측) — vol1=Parts 1–50, vol2=Parts 51–199.
# 이 범위 밖 Part 검출은 본문 교차참조(예: Part 50 본문의 "§ 52.1")이므로 *반드시* 배제한다
# (배제 안 하면 phantom Part 가 진짜 Part 의 end 경계를 잘라먹는다 — 실측 확인).
_VOL_PART_RANGE: dict[str, tuple[int, int]] = {
    "title10-vol1": (1, 50),
    "title10-vol2": (51, 199),
}


def _os_post(endpoint: str, index: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _list_title10_packages(endpoint: str, index: str) -> list[str]:
    """CFR-*-title10-vol{1,2} packageId 전부(원자력 = NRC Chapter I = Parts 1–199)."""
    body = {
        "size": 0,
        "query": {"term": {"collection": "10CFR"}},
        "aggs": {"pkg": {"terms": {
            "field": "doc_metadata.packageId.keyword", "size": 500,
        }}},
    }
    res = _os_post(endpoint, index, body)
    pkgs = [b["key"] for b in res["aggregations"]["pkg"]["buckets"]]
    return sorted(p for p in pkgs if _PKG_RE.match(p))


def _scan_volume(
    endpoint: str, index: str, pkg: str
) -> tuple[dict[int, int], dict[int, dict[int, int]], dict[int, int], int]:
    """볼륨 1개를 페이지순 전수 스캔 → (header_starts, page_section_counts, reserved, max_page).

    header_starts[part_no] = 첫 clean PART 헤더가 등장한 page_start (신호 A).
    page_section_counts[page][part_no] = 그 페이지의 §{part}.{sec} 출현 카운트 (신호 B 입력).
    reserved[part_no] = PART {N} [RESERVED] 헤더 page (본문 없음).
    """
    header_starts: dict[int, int] = {}
    page_secs: dict[int, dict[int, int]] = {}
    reserved: dict[int, int] = {}
    max_page = 0
    after: list | None = None
    while True:
        body: dict[str, Any] = {
            "size": 1000,
            "_source": ["page_start", "page_end", "text"],
            "query": {"term": {"doc_metadata.packageId.keyword": pkg}},
            "sort": [{"page_start": "asc"}, {"_id": "asc"}],
        }
        if after is not None:
            body["search_after"] = after
        res = _os_post(endpoint, index, body)
        hits = res["hits"]["hits"]
        if not hits:
            break
        for h in hits:
            s = h["_source"]
            pg = int(s.get("page_start") or 0)
            pe = int(s.get("page_end") or pg)
            max_page = max(max_page, pe, pg)
            text = s.get("text") or ""
            for m in _RESERVED_RE.finditer(text):
                if m.group(2):  # subpart-letter(50A 등) 는 Part 가 아님 — 건너뜀
                    continue
                reserved.setdefault(int(m.group(1)), pg)
            for m in _HEADER_RE.finditer(text):
                if m.group(2):
                    continue
                pn = int(m.group(1))
                if pn not in header_starts:
                    header_starts[pn] = pg
            for m in _SECTION_RE.finditer(text):
                pn = int(m.group(1))
                page_secs.setdefault(pg, {})[pn] = page_secs.get(pg, {}).get(pn, 0) + 1
        after = hits[-1]["sort"]
    return header_starts, page_secs, reserved, max_page


_DENSITY_MIN = 3  # §{pn}.{M} 가 그 페이지에서 *지배적*이려면 최소 카운트(교차참조 노이즈 배제).


def _density_start(page_secs: dict[int, dict[int, int]], pn: int) -> int | None:
    """§{pn}.{M} body-section 이 *지배적*(그 페이지 최다 카운트·≥_DENSITY_MIN)으로 처음 등장하는
    페이지(신호 B). 볼륨 Part 범위(_VOL_PART_RANGE)는 이미 호출부가 걸러 phantom 은 없다.

    단조성(Part 번호↔페이지) 은 가정하지 않는다 — 인덱스 page_start 서수가 Part 번호 순서와
    완전히 일치하지 않을 수 있다(실측: vol2 에서 Part 73 본문이 71/72 보다 앞 페이지에 옴)."""
    for pg in sorted(page_secs):
        counts = page_secs[pg]
        if pn in counts and counts[pn] == max(counts.values()) and counts[pn] >= _DENSITY_MIN:
            return pg
    return None


def _resolve_starts(header_starts: dict[int, int],
                    page_secs: dict[int, dict[int, int]],
                    want_parts: list[int], max_page: int) -> tuple[dict[int, int], dict[int, str]]:
    """Part→시작페이지 산출. Part 당 헤더 앵커(가장 정밀) 우선, 없으면 dominant-density.

    단조성을 강제하지 않는다(인덱스 page 서수가 Part 번호순과 어긋날 수 있음). 대신 볼륨 Part
    범위 필터(호출부)가 교차참조 phantom 을 제거하고, density 의 지배성+카운트 임계가 본문 시작을
    구분한다. end 경계는 호출부 `_resolve_part_ends` 가 *페이지순* 다음 Part 로 계산한다."""
    how: dict[int, str] = {}
    starts: dict[int, int] = {}
    for pn, pg in header_starts.items():
        starts[pn] = pg
        how[pn] = "header"
    for pn in want_parts:
        if pn in starts:
            continue
        d = _density_start(page_secs, pn)
        if d is not None:
            starts[pn] = d
            how[pn] = "density"
    return starts, how


def _resolve_part_ends(starts: dict[int, int], max_page: int) -> dict[int, tuple[int, int]]:
    """Part 시작 페이지(전체, want 무관) → (page_start, page_end). end = *다음 Part* start − 1
    (다음 Part 가 want 가 아니어도 경계로 쓴다 — 정확한 끝). 마지막 = 볼륨 max page."""
    ordered = sorted(starts.items(), key=lambda kv: (kv[1], kv[0]))
    spans: dict[int, tuple[int, int]] = {}
    for i, (pn, ps) in enumerate(ordered):
        pe = max(ps, ordered[i + 1][1] - 1) if i + 1 < len(ordered) else max(ps, max_page)
        spans[pn] = (ps, pe)
    return spans


def _newest_per_volbase(pkgs: list[str]) -> dict[str, list[str]]:
    """vol_base 별로 연차(연도) 최신 _NEWEST_EDITIONS 판만 남긴다."""
    by_base: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for p in pkgs:
        m = _PKG_RE.match(p)
        if m:
            by_base[m.group(2)].append((int(m.group(1)), p))
    out: dict[str, list[str]] = {}
    for base, lst in by_base.items():
        out[base] = [p for _, p in sorted(lst, reverse=True)[:_NEWEST_EDITIONS]]
    return out


def build(endpoint: str, index: str) -> dict:
    pkgs = _list_title10_packages(endpoint, index)
    newest = _newest_per_volbase(pkgs)
    print(f"[build] title10 packages: {len(pkgs)}; using newest {_NEWEST_EDITIONS} "
          f"per vol: {dict((k, v) for k, v in newest.items())}", file=sys.stderr)

    out_parts: dict[str, dict] = {}
    coverage_log: list[str] = []
    reserved_seen: dict[int, str] = {}

    want_by_vol: dict[str, list[int]] = defaultdict(list)
    for pn in NUCLEAR_PARTS:
        want_by_vol["title10-vol1" if pn <= 50 else "title10-vol2"].append(pn)

    for vol_base, vol_pkgs in newest.items():
        # 최신판 합의: Part 시작 페이지를 판별로 산출한 뒤, Part 별 최빈(동률이면 최신=첫 pkg)값.
        per_edition_starts: list[dict[int, int]] = []
        per_edition_how: list[dict[int, str]] = []
        max_page = 0
        lo_part, hi_part = _VOL_PART_RANGE[vol_base]
        in_range = lambda pn: lo_part <= pn <= hi_part  # noqa: E731
        for pkg in vol_pkgs:
            header_starts, page_secs, reserved, mp = _scan_volume(endpoint, index, pkg)
            max_page = max(max_page, mp)
            # 볼륨 Part 범위 밖(교차참조 phantom)은 헤더·섹션 양쪽에서 제거 — end 경계 보호.
            header_starts = {pn: pg for pn, pg in header_starts.items() if in_range(pn)}
            page_secs = {pg: {pn: c for pn, c in d.items() if in_range(pn)}
                         for pg, d in page_secs.items()}
            page_secs = {pg: d for pg, d in page_secs.items() if d}
            # end 경계 정확도를 위해 *범위 내 모든* Part(원자력 무관 포함)를 백스톱 want 로 넣는다.
            all_parts = sorted(set(header_starts) | {pn for d in page_secs.values() for pn in d})
            starts, how = _resolve_starts(header_starts, page_secs, all_parts, mp)
            per_edition_starts.append(starts)
            per_edition_how.append(how)
            for pn in reserved:
                reserved_seen[pn] = vol_base
            print(f"[build]   {pkg}: {len(header_starts)} headers, "
                  f"{len(starts)} resolved starts, max_page={mp}", file=sys.stderr)

        # Part 별 최신판 합의 시작 페이지(첫 판=최신 우선).
        consensus: dict[int, int] = {}
        chow: dict[int, str] = {}
        all_pns = sorted({pn for s in per_edition_starts for pn in s})
        for pn in all_pns:
            vals = [s[pn] for s in per_edition_starts if pn in s]
            best = max(set(vals), key=lambda v: (vals.count(v), -vals.index(v) if v in vals else 0)) \
                if len(set(vals)) > 1 else vals[0]
            # 동률·편차 시 최신판(첫 pkg) 값 우선.
            consensus[pn] = per_edition_starts[0].get(pn, best)
            chow[pn] = per_edition_how[0].get(pn, "consensus")
            if len(set(vals)) > 1 and (max(vals) - min(vals)) > 10:
                coverage_log.append(
                    f"  NOTE {vol_base} Part {pn}: start {sorted(set(vals))} across editions "
                    f"(using {consensus[pn]}, {chow[pn]})")

        spans = _resolve_part_ends(consensus, max_page)
        for pn in want_by_vol[vol_base]:
            if pn not in spans:
                continue
            ps, pe = spans[pn]
            out_parts[str(pn)] = {
                "vol_base": vol_base, "page_start": ps, "page_end": pe,
                "title": NUCLEAR_PARTS[pn], "extracted_by": chow.get(pn, "?"),
            }

    # Part 53 등 reserved 처리 — 본문 없어 span 미산출, 명시 기록(설계 §2.2).
    for pn, vol_base in reserved_seen.items():
        if pn in NUCLEAR_PARTS and str(pn) not in out_parts:
            out_parts[str(pn)] = {
                "vol_base": vol_base, "reserved": True, "title": NUCLEAR_PARTS[pn],
            }

    # 완전성 게이트 — §2 Tier1/2 Part 누락을 명시(silent 금지). reserved 는 누락 아님.
    missing = [pn for pn in NUCLEAR_PARTS if str(pn) not in out_parts]
    for pn in missing:
        coverage_log.append(f"  MISSING Part {pn} ({NUCLEAR_PARTS[pn][:40]}) — no header/density")

    print("[build] coverage:", file=sys.stderr)
    for line in coverage_log or ["  (all nuclear parts resolved)"]:
        print(line, file=sys.stderr)
    print(f"[build] resolved {len([p for p in out_parts.values() if not p.get('reserved')])}"
          f"/{len(NUCLEAR_PARTS)} nuclear parts ({len(missing)} missing, "
          f"{len([p for p in out_parts.values() if p.get('reserved')])} reserved)", file=sys.stderr)

    return {
        "schema_version": "1",
        "source": f"nrc-all-v1 text PART headers + section density, Title 10 vol1/vol2, "
                  f"newest {_NEWEST_EDITIONS} editions",
        "parts": dict(sorted(out_parts.items(), key=lambda kv: int(kv[0]))),
        "missing_parts": sorted(missing),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:9200")
    ap.add_argument("--index", default="nrc-all-v1")
    ap.add_argument("--out", default="backend/app/application/intake/_10cfr_part_pages.json")
    args = ap.parse_args()
    data = build(args.endpoint, args.index)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[build] wrote {args.out} ({len(data['parts'])} parts)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
