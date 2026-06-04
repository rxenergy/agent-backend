from __future__ import annotations

import re
from typing import Iterable

from app.domain.classification import (
    DEFAULT_DEPTH,
    DEFAULT_OBJECT,
    ClassificationResult,
)
from app.domain.interaction import ChatTurn

# 단순 키워드 기반 분류기. 한국어/영어 혼용 환경 기준.
# LLM classifier가 비활성일 때 또는 hybrid의 1차로 사용된다.

_VENDOR_KEYWORDS = (
    "nuscale", "smart", "i-smr", "ismr", "bwrx", "bwrx-300",
    "x-energy", "xe-100", "natrium", "kp-fhr",
    "노형", "vendor", "설계", "design",
)
_REGULATION_KEYWORDS = (
    "rg ", "rg1.", "10 cfr", "10cfr", "nureg", "kins", "고시",
    "regulatory guide", "규제", "법령", "규정",
    # 코퍼스 실재 규제 식별자(NRC 지침) — goldset 계획 W-A.
    "gdc", "srp", "dsrs", "federal register",
)
_RAI_KEYWORDS = (
    "rai #", "rai#", "rai ", "request for additional information",
    # "심사" 제거(D-1): SRP "심사 기준"(O2 규제)과 RAI "심사기록"(O3) 충돌 →
    # O3 는 감사/FMEA/심사의견/RAI 식별자로만 검출. SRP 는 W-A entity 로 O2 확정.
    "감사", "audit", "fmea", "심사의견", "심사 의견",
)
_RELATION_KEYWORDS = (
    "관계", "매핑", "어떻게 만족", "어떻게 충족", "vs", "대비",
    "compared", "compliance", "how does", "어떻게 적용",
)

_OVERVIEW_KEYWORDS = (
    "현황", "통계", "분류", "리스트", "목록", "패턴", "빈도",
    "overview", "summary", "주요", "전반",
)
_TECHNICAL_KEYWORDS = (
    "설계", "메커니즘", "원리", "수치", "파라미터", "기준값", "성능",
    "design", "mechanism", "spec", "specification", "특징", "동작",
    # goldset 계획 W-C — 기술 디테일·인과·절차·근거 표현 보강.
    "기술", "내용", "특성", "방식", "방법", "이유", "근거", "처리", "작동",
)
_FORMAL_KEYWORDS = (
    "원문", "정의", "조항", "요건", "정확한", "공식",
    "definition", "verbatim", "clause", "section",
    # goldset 계획 W-C — 원문/질의문 전문·인용 문구 표현 보강.
    "전문", "문구", "질의문",
)

# Entity 추출 정규식
_RE_RAI = re.compile(r"RAI\s*#?\s*(\d+)", re.IGNORECASE)
# RAI/감사 코드형 식별자(goldset 계획 W-B). 코드는 대문자라 IGNORECASE 미적용
# (일반 하이픈 토큰 오탐 축소 — Q-1 interim).
# trailing `\b` 미사용: 한국어 조사(…22"의")가 Unicode word char 라 \b 가 깨진다
# (_RE_RG 와 동일 이슈). leading `\b` 만 유지.
_RE_RAI_CODE = re.compile(r"\b[A-Z]{2,5}-[A-Z]{1,3}-\d+")   # DWO-SC-22
_RE_AUDIT_CODE = re.compile(r"\b[A-Z]-\d+(?:\.\d+)+-\d+")    # A-5.2.1.1-4 (SDAA 감사 질의)
# Trailing `\b` removed: Korean particles like "의/을" are Unicode word chars,
# so `\b` after a digit fails when a Korean particle follows.
_RE_RG = re.compile(r"\bRG\s*\d+(?:\.\d+)?", re.IGNORECASE)
# CFR 조항 세분 포착: Part·하위 단락 (b)/(a)(1)(i) 형 (goldset 계획 W-A).
_RE_CFR = re.compile(r"10\s*CFR\s*(?:Part\s*)?\d+(?:\.\d+)*(?:\([a-z0-9]+\))*", re.IGNORECASE)
_RE_KINS = re.compile(r"KINS\s*[가-힣A-Z0-9\-]+", re.IGNORECASE)
# NRC 규제 식별자(goldset 계획 W-A). GDC=10 CFR 50 App A(규제). SRP/DSRS=심사지침.
_RE_GDC = re.compile(r"\bGDC\s*\d+", re.IGNORECASE)
_RE_SRP = re.compile(r"\bSRP(?:\s+Chapter)?\s+\d+(?:\.\d+)*", re.IGNORECASE)
_RE_DSRS = re.compile(r"\bDSRS(?:\s+Section)?\s*\d+(?:\.\d+)*", re.IGNORECASE)
# PDC(Principal Design Criteria)는 노형 구성물 — 규제 아님(Q-4). 별도 종류로
# 추출만 하고 객체 점수엔 가산하지 않는다(neutral).
_RE_PDC = re.compile(r"\bPDC\s*\d+", re.IGNORECASE)
_VENDOR_NAMES = (
    "NuScale", "SMART", "i-SMR", "BWRX-300", "X-energy",
    "Xe-100", "Natrium", "KP-FHR",
)


class RuleClassifier:
    backend = "rule"

    async def classify(
        self,
        query_text: str,
        chat_history: Iterable[ChatTurn] = (),
    ) -> ClassificationResult:
        q = query_text.lower()
        # 점수 기반 — 정확 매칭과 빈도를 합산
        o_scores = {
            "O1": _hits(q, _VENDOR_KEYWORDS),
            "O2": _hits(q, _REGULATION_KEYWORDS),
            "O3": _hits(q, _RAI_KEYWORDS),
            "O4": _hits(q, _RELATION_KEYWORDS),
        }
        d_scores = {
            "D1": _hits(q, _OVERVIEW_KEYWORDS),
            "D2": _hits(q, _TECHNICAL_KEYWORDS),
            "D3": _hits(q, _FORMAL_KEYWORDS),
        }
        # RAI 번호가 있으면 O3 가중치 부스트
        entities = _extract_entities(query_text)
        if entities.get("rai_numbers"):
            o_scores["O3"] += 3
        if entities.get("regulation_ids"):
            o_scores["O2"] += 2
        if entities.get("vendors"):
            o_scores["O1"] += 2
        # 둘 이상의 강한 객체가 동시 등장하면 관계 질문으로 본다.
        # vendor + regulation, vendor + rai, regulation + rai 중 하나라도 entity 가
        # 동시에 잡히면 O4를 가장 큰 객체보다 1점 위로 올린다.
        present_kinds = sum(1 for k in ("vendors", "regulation_ids", "rai_numbers") if entities.get(k))
        if present_kinds >= 2:
            top_other = max(o_scores[o] for o in ("O1", "O2", "O3"))
            o_scores["O4"] = max(o_scores["O4"], top_other) + 1
        else:
            strong_objects = sum(1 for v in o_scores.values() if v >= 2)
            if strong_objects >= 2:
                o_scores["O4"] += 2

        scenario_object, obj_conf = _argmax(o_scores, default=DEFAULT_OBJECT)
        scenario_depth, dep_conf = _argmax(d_scores, default=DEFAULT_DEPTH)
        confidence = min(obj_conf, dep_conf)

        return ClassificationResult(
            scenario_object=scenario_object,
            scenario_depth=scenario_depth,
            entities=entities,
            confidence=confidence,
            object_confidence=obj_conf,
            depth_confidence=dep_conf,
            classifier_backend=self.backend,
        )


def _hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for k in keywords if k in text)


def _argmax(scores: dict[str, int], *, default: str) -> tuple[str, float]:
    """returns (label, confidence∈[0,1]).

    Confidence policy: top score / (top score + runner-up + 1).
    All-zero → default with confidence 0.0.
    """
    if not any(scores.values()):
        return default, 0.0
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_label, top = ordered[0]
    runner = ordered[1][1] if len(ordered) > 1 else 0
    conf = top / (top + runner + 1)
    return top_label, round(conf, 3)


def _extract_entities(text: str) -> dict[str, list[str]]:
    # RAI: 번호형(RAI #NNNN) + 코드형(DWO-SC-22) + 감사 질의 코드(A-5.2.1.1-4).
    rai = [m.group(0).strip() for m in _RE_RAI.finditer(text)]
    rai += [m.group(0).strip() for m in _RE_RAI_CODE.finditer(text)]
    rai += [m.group(0).strip() for m in _RE_AUDIT_CODE.finditer(text)]
    rg = [m.group(0).strip() for m in _RE_RG.finditer(text)]
    cfr = [m.group(0).strip() for m in _RE_CFR.finditer(text)]
    kins = [m.group(0).strip() for m in _RE_KINS.finditer(text)]
    gdc = [m.group(0).strip() for m in _RE_GDC.finditer(text)]
    srp = [m.group(0).strip() for m in _RE_SRP.finditer(text)]
    dsrs = [m.group(0).strip() for m in _RE_DSRS.finditer(text)]
    pdc = [m.group(0).strip() for m in _RE_PDC.finditer(text)]
    vendors = [v for v in _VENDOR_NAMES if v.lower() in text.lower()]
    out: dict[str, list[str]] = {}
    if vendors:
        out["vendors"] = vendors
    # GDC/SRP/DSRS 는 NRC 규제 식별자 → regulation_ids 로 합류(O2 가산 대상).
    reg_ids = rg + cfr + kins + gdc + srp + dsrs
    if reg_ids:
        out["regulation_ids"] = reg_ids
    if rai:
        # dedup(코드와 번호형 중복 매칭 방지), 순서 보존.
        out["rai_numbers"] = list(dict.fromkeys(rai))
    if pdc:
        # 객체 점수 중립 — 노형 설계기준(O1 성격)이라 regulation_ids 미합류(Q-4).
        out["design_criteria"] = pdc
    return out
