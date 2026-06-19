from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.application.agents.events import LazyReasoning, current_emitter
from app.application.intake.reasoning_capture import extract_reasoning, stream_capture
from app.domain.spec_driven import AnswerSpec, FormulatedQuery
from app.observability import openinference as oi
from app.observability.logging import get_logger
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort, LLMUnavailableError

_TRACER = get_tracer("intake")
_LOG = get_logger("intake.spec_driven_query")

# spec_driven_v1 N2 — Query Formulation Node. Answer Spec 의 슬롯·명시적 참조를 *구체
# 검색쿼리*(슬롯당 1개)로 옮긴다(설계 §3.2). 리터럴 키워드 보존 + 명시적 참조 verbatim
# 합류(BM25 lexical 앵커) + collection boost(가산만, hard filter 아님 — 사용자 #2).
#
# 모델(utility LLM + json_schema)이 쿼리를 *표현*하고, 결정론 layer 가 두 안전망을 건다:
#   (1) 모든 explicit_reference 가 ≥1 쿼리의 query_text 에 verbatim 으로 들어가게 보장
#       (모델이 슬롯 매핑을 놓쳐도 lexical 앵커가 유실되지 않게 — 본 variant 의 load-bearing
#       신호). (2) reference prefix 에서 collection boost 를 결정론적으로 유도(모델 누락 보정).
#
# 프롬프트·스키마는 registry(spec_driven_query_prompts)에서 SpecDrivenQuerySource 주입.

# 허용 collection 값 — corpus_map collections + nuscale_* 문서군(nrc-all-v1 keyword 필드).
# boost(target) 와 filter(filters) 양쪽 모드가 이 집합으로 검증된다.
_COLLECTIONS = frozenset({
    "10CFR", "DSRS", "FR", "RG", "SRP",
    "nuscale_Affidavit", "nuscale_Audit", "nuscale_DCA", "nuscale_etc", "nuscale_FSAR",
    "nuscale_Inspection", "nuscale_Letter", "nuscale_Meeting", "nuscale_RAI",
    "nuscale_SER", "nuscale_TechReport", "nuscale_Topical_Report",
})

# reference 토큰 → collection 유도(결정론, 대소문자 무시). GDC/Appendix 는 10 CFR 50 의
# 일부이므로 10CFR, NUREG-0800 은 SRP 의 문서번호.
_COLLECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("10 cfr", "10CFR"),
    ("10cfr", "10CFR"),
    ("gdc", "10CFR"),
    ("appendix", "10CFR"),
    ("app k", "10CFR"),
    ("app b", "10CFR"),
    ("nureg-0800", "SRP"),
    ("srp", "SRP"),
    ("dsrs", "DSRS"),
    ("federal register", "FR"),
    ("reg guide", "RG"),
    ("rg ", "RG"),
    ("rg-", "RG"),
)


def _derive_collection(text: str) -> str | None:
    low = text.lower()
    for needle, coll in _COLLECTION_PATTERNS:
        if needle in low:
            return coll
    return None


# === 검색 스코프 메타데이터 채널 (설계 spec_driven_search_scope_metadata.design.v1) ===
# collection 외 status/design/canonical_id 를 N3 retrieval.search 의 filters/target 에
# *인덱스 필드 경로* 키로 싣는다. _opensearch_hybrid 가 임의 필드명을 term/terms 로
# 변환하므로 DSL 빌더 수정 없이 동작한다. status↔규제 / design↔NuScale 배타성(§0-C)을
# 코드가 강제한다: 부적합 collection 슬롯에 실린 채널은 *무시*(빈값 필터 → 0건 방지).
_STATUS_FIELD = "doc_metadata.std_status.keyword"
_DESIGN_FIELD = "doc_metadata.std_design.keyword"
_CANONICAL_FIELD = "doc_metadata.std_canonical_id.keyword"
_PAGE_RANGE_FIELD = "page_range"  # integer_range — 10CFR Part 페이지 구간 스코프(설계 §4).

# std_status 는 RG/SRP/DSRS 만 보유(10CFR/FR/nuscale_* 빈값).
_STATUS_VALUES = frozenset({
    "current", "history", "draft", "withdrawn", "AdditionalInformation",
})
_STATUS_COLLECTIONS = frozenset({"RG", "SRP", "DSRS"})

# std_design 은 nuscale_* 만 보유. 값 표기는 인덱스 적재 표기(언더스코어 없음 — 실측
# 확인: agg 결과 US600/US460/PreApp). PreApp=Pre-Application 단계 문서.
_DESIGN_VALUES = frozenset({"US460", "US600", "PreApp"})
_DESIGN_COLLECTION_PREFIX = "nuscale_"

# canonical_id 정규화 가능 형식(NRC_MANUAL 한정 — 데이터 설명 "canonical ID 규칙").
# doc_type prefix → (정규식, collection). 검증 통과 시에만 스코프로 승격.
_CANONICAL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("RG", re.compile(r"^RG-\d+\.\d+$")),
    ("SRP", re.compile(r"^SRP-\d+([.\-].+)?$")),
    ("DSRS", re.compile(r"^DSRS-\d+(\.\d+)*$")),
    ("10CFR", re.compile(r"^10CFR-Part[\w\-]+$")),
)


def _validate_canonical_id(cid: str, collection: str | None) -> str | None:
    """canonical_id 게이트(결정론) — 정규식 매칭 + doc_type prefix ↔ collection 정합.
    통과 시 cid 반환, 실패 시 None(버림 → lexical-only). collection 미지정이면 prefix 가
    매칭 정규식의 doc_type 과 같다고 보고 통과(모델이 collection 을 비웠어도 id 형식이
    규칙에 맞으면 승격 — prefix 자체가 collection 을 함의)."""
    if not cid:
        return None
    cid = cid.strip()
    for doc_type, pat in _CANONICAL_PATTERNS:
        if pat.match(cid):
            # prefix 정합: collection 이 주어졌으면 doc_type 과 일치해야 한다.
            if collection and collection != doc_type:
                return None
            return cid
    return None


# === 10CFR Part→Page 스코프 맵 (설계 spec_driven_10cfr_part_page_map.design.v1) ============
# 문제: 10CFR std_canonical_id 는 govinfo 연차판 *볼륨* 단위(`10CFR-Part1-50` 등 ~1,000p
# 묶음)라 Part 단위 스코프가 불가능하다(chunk 의 part_no 는 0.3%만 채워져 실측상 못 씀).
# 해법: 원자력 관련 Part 의 (vol_base, page_start, page_end) 정적 맵(scripts/build_10cfr_
# part_page_map.py 가 본문 PART 헤더 + §섹션 밀도로 추출)을 로드해, Part 식별 시 ① page_range
# 범위 필터 + ② 볼륨 canonical 키를 합성한다(교집합 = Part 페이지 구간만). 맵 miss/예약 Part
# 는 page_range 미합성 → 볼륨 canonical 또는 lexical-only 폴백(recall 안전).
_PART_PAGES_PATH = Path(__file__).with_name("_10cfr_part_pages.json")


def _load_part_pages() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_PART_PAGES_PATH.read_text(encoding="utf-8"))
        parts = data.get("parts")
        return parts if isinstance(parts, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}  # 맵 부재/손상 → 폴백(net-neutral — 기존 볼륨 canonical 동작).


_PART_PAGES: dict[str, dict[str, Any]] = _load_part_pages()

# canonical_id 에서 10CFR Part 번호 추출: `10CFR-Part50` / `10CFR-Part1-50`(볼륨, from-to).
_TENCFR_PART_RE = re.compile(r"^10CFR-Part0*(\d+)(?:-0*(\d+))?$", re.IGNORECASE)


def _resolve_10cfr_part_pages(cid: str) -> dict[str, Any] | None:
    """10CFR canonical 에서 *단일 Part* 를 식별해 맵 조회 결과를 반환(결정론).

    반환: {part, vol_canonical, page_range|None, reserved, resolved} | None(10CFR Part 아님).
    - 단일 Part(`10CFR-Part50`): 맵 hit 시 page_range + 그 Part 가 속한 볼륨 canonical.
    - 볼륨형(`10CFR-Part1-50`, from≠to): 이미 볼륨 키이므로 page 미합성(그대로 둠 — resolved=False).
    """
    m = _TENCFR_PART_RE.match(cid.strip())
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    if lo != hi:
        # 볼륨형(범위) — 이미 묶음 키. Part 단위 페이지 좁힘 대상 아님(그대로 캐노니컬 유지).
        return {"part": None, "vol_canonical": cid.strip(), "page_range": None,
                "reserved": False, "resolved": False}
    entry = _PART_PAGES.get(str(lo))
    if not entry:
        return {"part": lo, "vol_canonical": None, "page_range": None,
                "reserved": False, "resolved": False}  # 맵 miss → 폴백.
    if entry.get("reserved"):
        return {"part": lo, "vol_canonical": None, "page_range": None,
                "reserved": True, "resolved": False}  # 예약 Part(본문 없음, 예: Part 53).
    ps, pe = entry.get("page_start"), entry.get("page_end")
    vol_base = entry.get("vol_base")
    # 볼륨 canonical 키: vol1 → 10CFR-Part1-50, vol2 → 10CFR-Part51-199(인덱스 실측 표기).
    vol_canonical = {"title10-vol1": "10CFR-Part1-50",
                     "title10-vol2": "10CFR-Part51-199"}.get(vol_base)
    pr = ({"gte": int(ps), "lte": int(pe), "relation": "intersects"}
          if ps is not None and pe is not None else None)
    return {"part": lo, "vol_canonical": vol_canonical, "page_range": pr,
            "reserved": False, "resolved": pr is not None}


# === FSAR canonical_id (NuScale 신청자 문서 — design 스코프와 결합) ===================
# 설계 spec_driven_search_scope_metadata §5b/§9 + FSAR 챕터 검색.
#
# 인덱스 실측 표기(std_canonical_id.keyword):
#   FSAR-Part02-Ch{NN}  /  FSAR-Part02-T2-Ch{NN}     (같은 챕터가 두 표기로 갈림)
#   FSAR-Part02-T2-Sec{N.NN}[-App{N}A]               (섹션/부록 수준 — 일부)
#   FSAR-Part{01,07,08,09,10}                         (Part 2 외 — Tier 없음, Part 만)
#
# 핵심: Part 2 챕터는 exact term 으로 잡히지 않는다(-T2- 유무·하위 Section 분기). 그래서
# 챕터 단위 스코프는 **wildcard** `FSAR-Part02*Ch{NN}` 로 두 표기·하위섹션을 한 번에
# 흡수한다(인덱스 실측 확인). DSL 빌더가 값의 `*` 를 보고 wildcard 절로 변환한다.
#
# FSAR 챕터 의미 맵(코퍼스 DocumentTitle ground-truth — NUREG-0800/RG1.206 구조 +
# NuScale 특화 Ch20/21). 모델 프롬프트와 코드가 *같은 맵*을 공유한다(단일 진실원천):
# 프롬프트는 모델이 "ECCS→Ch06" 의미를 알게 하고, 코드는 모델이 낸 챕터 번호를 이 맵으로
# 검증(범위 밖이면 기각). 표현=모델(챕터 선택), 결정=코드(범위 검증·wildcard 조립).
_FSAR_CHAPTERS: dict[int, str] = {
    1: "Introduction and General Description of the Plant",
    2: "Site Characteristics",
    3: "Design of Structures, Systems, Components, and Equipment",
    4: "Reactor",
    5: "Reactor Coolant System and Connecting Systems",
    6: "Engineered Safety Features",
    7: "Instrumentation and Controls",
    8: "Electric Power",
    9: "Auxiliary Systems",
    10: "Steam and Power Conversion System",
    11: "Radioactive Waste Management",
    12: "Radiation Protection",
    13: "Conduct of Operations",
    14: "Initial Test Program / Verification and Validation",
    15: "Transient and Accident Analyses",
    16: "Technical Specifications",
    17: "Quality Assurance",
    18: "Human Factors Engineering",
    19: "Probabilistic Risk Assessment and Severe Accident Evaluation",
    20: "Mitigation of Beyond-Design-Basis Events",
    21: "Multi-Module Design Considerations",
}

# Part 2 외 FSAR Part 의미(코퍼스 DocumentTitle ground-truth). Tier 없음, Part 만.
_FSAR_PARTS_NON_TIER: dict[int, str] = {
    1: "General and Financial Information",
    7: "Exemptions",
    8: "License Conditions; ITAAC",
    9: "Withheld Information",
    10: "Quality Assurance Program Description",
}

# 모델이 낼 수 있는 FSAR canonical 입력형(관대 수용 후 코드가 정규화):
#   FSAR-Part02-Ch6 / FSAR-Part02-Ch06 / FSAR-Part2-Ch6 / FSAR-Ch6  → 챕터 wildcard
#   FSAR-Part1 / FSAR-Part07 …                                       → Part-only exact
_FSAR_CH_RE = re.compile(r"^FSAR-(?:Part0?2-)?(?:T2-)?Ch0?(\d{1,2})$", re.IGNORECASE)
_FSAR_PART_RE = re.compile(r"^FSAR-Part0?(\d{1,2})$", re.IGNORECASE)


def _validate_fsar_canonical(cid: str, collection: str | None) -> str | None:
    """FSAR canonical → 인덱스 검색 패턴(결정론). nuscale_FSAR 슬롯에서만 승격.

    - 챕터형(FSAR-...Ch{N}) → **wildcard** `FSAR-Part02*Ch{NN}`(zero-pad). 번호가
      _FSAR_CHAPTERS(1~21) 밖이면 기각(None). -T2- 유무·하위 Section 을 흡수.
    - Part-only(FSAR-Part{N}, N≠2) → exact `FSAR-Part{NN}`. _FSAR_PARTS_NON_TIER 에
      있는 Part 만(1/7/8/9/10). Part2 단독(챕터 없음)은 collection=nuscale_FSAR 가
      이미 전체 FSAR 라 중복 → 기각(채널 의미 없음).
    실패 시 None(버림 → lexical-only, recall 안전)."""
    if not cid:
        return None
    cid = cid.strip()
    # FSAR 는 nuscale_FSAR collection 에서만 의미가 있다(배타 — design 과 같은 군).
    if collection and collection != "nuscale_FSAR":
        return None
    m = _FSAR_CH_RE.match(cid)
    if m:
        ch = int(m.group(1))
        if ch in _FSAR_CHAPTERS:
            return f"FSAR-Part02*Ch{ch:02d}"  # wildcard — DSL 빌더가 wildcard 절로.
        return None  # 범위 밖 챕터 → 기각
    m = _FSAR_PART_RE.match(cid)
    if m:
        part = int(m.group(1))
        if part in _FSAR_PARTS_NON_TIER:
            return f"FSAR-Part{part:02d}"  # exact(Tier 없음)
        return None  # Part2(챕터 없음) 또는 미지 Part → 기각
    return None


class QueryFormulator:
    """N2 — Query Formulation. 프롬프트·스키마는 registry 에서 주입(SpecDrivenQuerySource)."""

    version = "spec_driven/query/v1"

    def __init__(
        self,
        llm: LLMPort,
        *,
        prompt_body: str,
        schema: dict | None = None,
        model_options: dict | None = None,
        policy_hash: str | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt_body
        self._schema = schema
        self._model_options = dict(model_options or {"temperature": 0.0})
        self._policy_hash = policy_hash

    async def formulate(
        self, query_text: str, spec: AnswerSpec, *, reasoning_label: str | None = None,
        persona_profile: str | None = None,
    ) -> tuple[tuple[FormulatedQuery, ...], str]:
        prompt = (
            self._prompt
            .replace("{query}", query_text)
            .replace("{spec}", _render_spec(spec))
        )
        # 페르소나 프로필(composer_persona_framework.design.v1 §6.3) — composer 페르소나
        # variant 가 자기 fragment 를 넘기면 검색 설계 프롬프트 앞에 prepend 한다(같은 fragment
        # 의 답 척추·collection 우선순위를 모델이 검색 라우팅 우선순위로 읽는다 — soft boost,
        # 사용자 결정: 모델 판단). 정적 body 불변(policy_hash 안정), 동적 입력만 prepend.
        # facet→collection 라우팅 값·status/design 배타성·canonical_id 게이트는 페르소나
        # 무관(governance §5.1 — 근거·cite 페르소나 불변). 중립(None)이면 현행 동작 불변.
        if persona_profile:
            prompt = persona_profile.rstrip() + "\n\n" + prompt
        with _TRACER.start_as_current_span("intake.spec_driven_query") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            if self._policy_hash:
                span.set_attribute("query_formulation.policy_hash", self._policy_hash)
            method = "llm"
            queries: tuple[FormulatedQuery, ...] = ()
            try:
                grammar = (
                    GrammarSpec(kind="json_schema", value=self._schema)
                    if self._schema else None
                )
                # emitter 활성 시 streaming(native CoT→thinking, 없으면 reasoning 필드
                # backstop) — N1 과 동형(설계 D2/D3). 비활성이면 현행 non-stream.
                em = current_emitter()
                lazy = LazyReasoning(reasoning_label) if em.active else None
                if lazy is not None:
                    res = await stream_capture(
                        self._llm, prompt,
                        model_options=dict(self._model_options),
                        grammar=grammar, lazy=lazy,
                    )
                else:
                    res = await self._llm.generate(
                        prompt, model_options=dict(self._model_options),
                        grammar=grammar,
                    )
                oi.set_llm(
                    span, model_name=res.model_id, prompt=prompt, completion=res.text,
                    prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(res.token_usage.get("completion_tokens", 0)),
                )
                if lazy is not None and not lazy.emitted:
                    await lazy.feed(extract_reasoning(res.text))
                queries = _parse(res.text)
            except LLMUnavailableError as exc:
                # 외부 요소(LLM 미가용)는 파싱불가(내부)와 구분해 명시 추적 — fallback
                # 쿼리로 떨어진 *이유*가 외부 미가용임을 span/로그에 남긴다(silent degrade
                # 사각지대 제거). trace_id 는 structlog _add_trace_context 가 자동 주입.
                span.set_attribute("query_formulation.upstream_error", str(exc)[:500])
                span.record_exception(exc)
                _LOG.warning("query_formulation_llm_unavailable",
                             upstream_error=str(exc)[:500],
                             error_type=type(exc).__name__,
                             model_id=getattr(self._llm, "model_id", "unknown"))
                queries = ()
            except Exception:  # noqa: BLE001 — 파싱불가 → 결정론 fallback
                queries = ()
            if not queries:
                method = "fallback"
                queries = _fallback_queries(query_text, spec)
            # 안전망 (1)·(2)·(3)·(4): refs verbatim 보장(스코프된 ref 제외) + collection
            # boost 유도 + 스코프된 문서명 제거 + 중복 제거.
            queries = _ensure_references(queries, spec.explicit_references)
            queries = _attach_targets(queries)
            # (4) 스코프(=filter)된 문서명을 query_text 에서 제거 — _ensure_references /
            # 모델이 남긴 verbatim 문서명을 *마지막에* 떼어낸다. _attach_targets 의 boost
            # 유도가 query_text 의 reference 토큰을 읽으므로 그 *뒤*에 둔다(유도 비파괴).
            queries = _strip_scoped_references(queries, spec.explicit_references)
            queries = _dedup_queries(queries)
            span.set_attribute("query_formulation.method", method)
            span.set_attribute("query_formulation.num_queries", len(queries))
            oi.set_io(span, output_value={
                "method": method, "num_queries": len(queries),
                "queries": [q.query_text for q in queries],
            })
            return queries, method


def _render_spec(spec: AnswerSpec) -> str:
    lines = [
        f"intent: {spec.intent}",
        f"governing_normative_class: {spec.governing_normative_class or 'null'}",
        f"explicit_references: {', '.join(spec.explicit_references) or '(none)'}",
        "required_slots:",
    ]
    for s in spec.required_slots:
        # 책임 재분배(split.design.v1): N1 은 *개념 수준* 검색 의도(scope_hint)만 주고,
        # N2 가 그것을 검색 주소(collection·토큰·canonical_id)로 번역한다. scope_hint(v2)
        # 우선, 비면 keywords(v1) 로 fallback(회귀 0). facet 은 N1 소유 라벨 — N2 가 rule 9
        # (facet→collection 표)로 라우팅 신호에 쓴다.
        intent_str = s.scope_hint or (", ".join(s.keywords) or "(none)")
        tags = []
        if s.facet:
            tags.append(f"facet={s.facet}")
        # expected_authority 는 deprecated(facet 에서 N2 가 도출) — v1 슬롯에만 잔존 노출.
        if s.expected_authority and not s.scope_hint:
            tags.append(f"authority={s.expected_authority}")
        tag = (" | " + " | ".join(tags)) if tags else ""
        lines.append(f"- {s.name}: search_intent=[{intent_str}]{tag} | {s.description}"
                     .rstrip())
    return "\n".join(lines)


def _parse(text: str) -> tuple[FormulatedQuery, ...]:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return ()
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, dict):
        return ()
    raw = data.get("queries")
    if not isinstance(raw, list):
        return ()
    out: list[FormulatedQuery] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        qt = str(q.get("query_text") or "").strip()
        if not qt:
            continue
        slot = str(q.get("slot_name") or "").strip() or "query"
        target: dict[str, list[str]] = {}
        filters: dict[str, Any] = {}

        def _put(field: str, value: str, mode: str) -> None:
            # mode=filter → hard-scope(filters), 그 외(boost/누락) → 가산 boost(target).
            (filters if mode == "filter" else target)[field] = [value]

        # (1) collection — 기존 채널. 역할 구분의 1차 신호.
        coll_raw = q.get("collection")
        collection = coll_raw.strip() if isinstance(coll_raw, str) else None
        if collection in _COLLECTIONS:
            _put("collection", collection,
                 str(q.get("collection_mode") or "boost").strip().lower())
        else:
            collection = None  # enum 외/누락은 미설정으로 정규화(아래 정합 게이트 입력).

        # 무시된 채널 감사 — 모델이 *값을 냈는데* 배타성/게이트로 버려진 경우만 기록한다
        # (silent drop 금지 — 원칙 6). 모델이 애초에 안 낸 채널은 drop 이 아니므로 미기록.
        audit: dict[str, Any] = {}

        # (2) status — 규제 collection(RG/SRP/DSRS)에만 합성(§4.3 배타성 강제).
        status = q.get("status")
        if isinstance(status, str) and status in _STATUS_VALUES:
            if collection in _STATUS_COLLECTIONS:
                _put(_STATUS_FIELD, status,
                     str(q.get("status_mode") or "boost").strip().lower())
            else:
                audit["status_dropped"] = True  # 비규제 collection 에 status → 무시.

        # (3) design — nuscale_* collection 에만 합성(§5.3 배타성 강제).
        design = q.get("design")
        if isinstance(design, str) and design in _DESIGN_VALUES:
            if collection and collection.startswith(_DESIGN_COLLECTION_PREFIX):
                _put(_DESIGN_FIELD, design,
                     str(q.get("design_mode") or "boost").strip().lower())
            else:
                audit["design_dropped"] = True  # 규제/미지정 collection 에 design → 무시.

        # (4) canonical_id — 게이트 통과 시만 승격. FSAR(NuScale, wildcard) 와
        # NRC_MANUAL(RG/SRP/DSRS/10CFR, exact) 두 검증기를 순차로 시도한다(§5b.2/§9).
        # FSAR 형(FSAR-... 접두)은 _validate_fsar_canonical 가 챕터→wildcard·범위 검증·
        # Part 검증을 한다. 그 외는 _validate_canonical_id(정규식+prefix 정합).
        cid = q.get("canonical_id")
        if isinstance(cid, str) and cid.strip():
            cid_s = cid.strip()
            cid_mode = str(q.get("canonical_id_mode") or "boost").strip().lower()
            if cid_s.upper().startswith("FSAR-"):
                valid = _validate_fsar_canonical(cid_s, collection)
            else:
                valid = _validate_canonical_id(cid_s, collection)
            if valid:
                # 10CFR 단일 Part → 볼륨 canonical 환산 + page_range 좁힘(설계 §3.3). page_range 는
                # 의미상 hard-scope 라 mode=filter 일 때만 filters 에 싣는다(boost 면 페이지 범위
                # 의미가 약해 생략 — 볼륨 boost 만). 맵 miss/예약/볼륨형은 page 미합성(폴백).
                part_info = (_resolve_10cfr_part_pages(valid)
                             if collection == "10CFR" or valid.upper().startswith("10CFR-")
                             else None)
                if part_info and part_info.get("vol_canonical"):
                    _put(_CANONICAL_FIELD, part_info["vol_canonical"], cid_mode)
                else:
                    _put(_CANONICAL_FIELD, valid, cid_mode)
                if part_info:
                    audit["canonical_part"] = part_info.get("part")
                    audit["page_range_resolved"] = bool(part_info.get("resolved"))
                    if part_info.get("reserved"):
                        audit["canonical_part_reserved"] = True
                    pr = part_info.get("page_range")
                    if pr and cid_mode == "filter":
                        filters[_PAGE_RANGE_FIELD] = pr
            else:
                audit["canonical_id_rejected"] = True  # 정규식/범위/prefix 불일치 → 버림.

        out.append(FormulatedQuery(slot_name=slot, query_text=qt,
                                   target=target, filters=filters,
                                   scope_audit=audit))
    return tuple(out)


def _fallback_queries(
    query_text: str, spec: AnswerSpec
) -> tuple[FormulatedQuery, ...]:
    """모델 부재/파싱불가 → 슬롯 검색의도로 슬롯당 쿼리 1개(결정론). scope_hint(v2) 우선,
    비면 keywords(v1) fallback. 슬롯이 없으면 원질의 1개."""
    out: list[FormulatedQuery] = []
    for s in spec.required_slots:
        qt = (s.scope_hint or " ".join(s.keywords)).strip()
        if not qt:
            continue
        out.append(FormulatedQuery(slot_name=s.name, query_text=qt))
    if not out:
        out.append(FormulatedQuery(slot_name="primary_evidence",
                                  query_text=query_text.strip()))
    return tuple(out)


def _reference_is_scoped(ref: str, q: FormulatedQuery) -> bool:
    """이 reference 가 쿼리 q 에서 *filter* 모드 스코프로 실현됐는지(=문서명이 lexical
    앵커로 불필요한지) 판정한다. boost-only/미스코프는 False(앵커 유지 — 사용자 결정).

    - canonical_id filter — exact 문서 타깃. ref 가 그 문서를 가리키면(파생 collection 이
      쿼리 collection 과 일치, 또는 collection 미지정) 스코프로 본다.
    - collection filter — ref 의 파생 collection 이 filter collection 과 같으면 스코프.

    `_CANONICAL_FIELD`/`collection` 이 target(boost)에만 있고 filters 에 없으면 hard-narrow
    가 아니므로 스코프 아님(False → query_text 의 ref 유지)."""
    ref_coll = _derive_collection(ref)
    filt_coll = q.filters.get("collection")
    coll = filt_coll[0] if isinstance(filt_coll, list) and filt_coll else None
    # collection filter 정합 — ref 가 그 collection 으로 파생되면 그 문서군으로 좁혀졌다.
    if coll and ref_coll == coll:
        return True
    # canonical_id filter — 특정 문서 버전 exact 타깃. ref 의 collection 이 쿼리
    # collection 과 모순되지 않으면(같거나, collection 미지정이면) 그 ref 를 좁힌 것으로 본다.
    if _CANONICAL_FIELD in q.filters:
        if coll is None or ref_coll is None or ref_coll == coll:
            return True
    return False


def _ensure_references(
    queries: tuple[FormulatedQuery, ...], refs: tuple[str, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (1): 각 explicit_reference 가 ≥1 쿼리의 query_text 에 verbatim 으로
    들어가게 보장 — **단, 어느 쿼리에서 filter 스코프로 실현된 ref 는 제외**(이미 모집단이
    그 문서로 좁혀져 문서명이 불필요하고 dense 유사도를 밋밋하게 만든다 — 사용자 결정,
    rule 3 scope). 미스코프 ref 만 누락 시 첫 쿼리에 append(lexical 앵커 유실 방지). 각
    쿼리의 references 도 채운다."""
    qlist = list(queries)
    if not qlist:
        return queries
    for ref in refs:
        if not ref:
            continue
        # 어느 쿼리든 이 ref 를 filter 스코프로 실현했으면 lexical 앵커가 불필요 — 강제
        # 주입 안 함(이미 query_text 에 있더라도 뒤의 _strip_scoped_references 가 떼낸다).
        if any(_reference_is_scoped(ref, q) for q in qlist):
            continue
        present = any(ref.lower() in q.query_text.lower() for q in qlist)
        if not present:
            first = qlist[0]
            qlist[0] = FormulatedQuery(
                slot_name=first.slot_name,
                query_text=f"{first.query_text} {ref}".strip(),
                target=first.target,
                filters=first.filters,
                references=first.references,
                scope_audit=first.scope_audit,
            )
    # 각 쿼리에 실제 포함된 refs 기록(감사용).
    rebuilt: list[FormulatedQuery] = []
    for q in qlist:
        present_refs = tuple(r for r in refs if r and r.lower() in q.query_text.lower())
        rebuilt.append(FormulatedQuery(
            slot_name=q.slot_name, query_text=q.query_text,
            target=q.target, filters=q.filters, references=present_refs,
            scope_audit=q.scope_audit,
        ))
    return tuple(rebuilt)


def _dedup_queries(
    queries: tuple[FormulatedQuery, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (3): 여러 슬롯이 *동일한* query_text+scope 로 검색하는 중복을 제거한다.
    소형 모델이 슬롯 다양화에 실패해 같은 query_text 를 N개 슬롯에 그대로 복제하는 경우
    (예: section_5_4_1_{structure,content,methodology} 가 전부 "NuScale FSAR section
    5.4.1") 동일 검색을 N회 반복해 컨텍스트 예산을 같은 chunk 로 낭비하고 다양성을
    떨어뜨린다. query_text(대소문자/공백 정규화) + collection scope 가 같으면 첫 쿼리만
    남기고 접는다. 접힌 슬롯명은 살아남는 쿼리의 references 가 아니라 slot_name 으로
    이미 floor 후보가 되며, 동일 검색이라 회수 chunk·score 가 같으므로 coverage 손실은
    없다(중복 검색만 제거). 프롬프트의 '슬롯별 다양화' 지시가 1차 방어, 이 함수가 백스톱.
    """
    seen: set[tuple[str, str]] = set()
    out: list[FormulatedQuery] = []
    for q in queries:
        # scope_key 는 *모든* 채널(collection·status·design·canonical_id)을 mode 와 함께
        # 포함한다. 동일 query_text 라도 스코프가 다르면 별개 검색이므로 접지 않는다
        # (예: collection RG + status current vs history 는 다른 모집단).
        parts: list[str] = []
        for field in ("collection", _STATUS_FIELD, _DESIGN_FIELD, _CANONICAL_FIELD):
            if field in q.filters:
                parts.append(f"filter:{field}={','.join(sorted(q.filters[field]))}")
            elif field in q.target:
                parts.append(f"boost:{field}={','.join(sorted(q.target[field]))}")
        # page_range(10CFR Part 좁힘) 는 dict 값 — 정렬된 항목으로 직렬화. 동일 query_text 라도
        # Part 페이지 구간이 다르면 별개 검색이므로 접지 않는다.
        if _PAGE_RANGE_FIELD in q.filters:
            pr = q.filters[_PAGE_RANGE_FIELD]
            parts.append(f"filter:{_PAGE_RANGE_FIELD}="
                         + ",".join(f"{k}={pr[k]}" for k in sorted(pr)))
        scope_key = "|".join(parts)
        key = (" ".join(q.query_text.lower().split()), scope_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return tuple(out)


def _attach_targets(
    queries: tuple[FormulatedQuery, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (2): query_text 의 reference 에서 collection boost 를 결정론적으로 유도해
    모델이 비운 target 을 보정(boost-only, recall-safe). 유도는 *절대 filter 로 escalate
    하지 않는다* — boost(target)만 쓴다. 모델이 collection 을 boost 든 filter 든 이미
    줬으면(둘 중 하나에 'collection' 키가 있으면) 그대로 두고 유도하지 않는다."""
    out: list[FormulatedQuery] = []
    for q in queries:
        target = dict(q.target)  # 기존 boost 채널(status/design/canonical 포함) 보존.
        has_collection = "collection" in target or "collection" in q.filters
        if not has_collection:
            coll = _derive_collection(q.query_text)
            if coll:
                target["collection"] = [coll]  # collection 만 *추가*(다른 채널 비파괴).
        out.append(FormulatedQuery(
            slot_name=q.slot_name, query_text=q.query_text,
            target=target, filters=q.filters, references=q.references,
            scope_audit=q.scope_audit,
        ))
    return tuple(out)


# reference verbatim 토큰을 query_text 에서 떼어낼 때 쓰는 매칭 — 공백 변형(연속 공백)을
# 흡수하고 토큰 경계를 존중한다(대소문자 무시). "10 CFR 50.61" 이 query_text 에 "10 cfr
# 50.61" 로 들어가도 잡고, "50.461" 같은 부분일치는 경계로 배제한다.
#
# 경계는 \b 가 아니라 **인접 영숫자 부정(lookaround)** 으로 둔다: `\b` 는 단어/비단어
# *전환* 이 있어야 매칭하는데, reference 가 괄호로 끝나거나(`10 CFR 50.46(b)`,
# `50.46(a)(1)(i)`) 시작하면 그 가장자리 문자(`)`)가 비단어라, 뒤에 공백(역시 비단어)이
# 오면 전환이 없어 `\b` 가 실패 → ref 가 안 떼어지고 query 에 남았다(괄호 하위호 누수
# 버그). (?<![\w]) / (?![\w]) 는 "직전/직후가 영숫자가 *아니면*" 으로 가장자리 문자
# 종류와 무관하게 성립하므로 괄호 끝 ref 도 잡고, "50.461"(직후가 숫자)은 여전히 배제한다.
def _ref_pattern(ref: str) -> "re.Pattern[str]":
    parts = [re.escape(tok) for tok in ref.split()]
    body = r"\s+".join(parts)
    return re.compile(r"(?<![\w])" + body + r"(?![\w])", re.IGNORECASE)


def _strip_scoped_references(
    queries: tuple[FormulatedQuery, ...], refs: tuple[str, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (4): filter 스코프로 실현된 reference 의 *문서명*을 query_text 에서 제거한다
    (사용자 결정 — rule 3 scope). 이미 collection/canonical_id filter 가 모집단을 그 문서로
    좁혔으므로 query_text 의 문서명은 검색 신호가 아니라 dense 유사도를 밋밋하게 만드는
    noise 다. 제거 후 query_text 가 *비면*(전부 문서명이었던 경우) 원본을 보존한다(빈 쿼리
    방지 — recall 안전). boost-only/미스코프 ref 는 _reference_is_scoped=False 라 유지된다.

    references(감사 필드)는 query_text 에 *남아 있는* ref 만 반영하도록 재계산한다 —
    스코프로 떼어낸 ref 는 더 이상 lexical 앵커가 아니므로 references 에서도 빠진다."""
    if not refs:
        return queries
    out: list[FormulatedQuery] = []
    for q in queries:
        scoped = [r for r in refs if r and _reference_is_scoped(r, q)]
        text = q.query_text
        if scoped:
            for ref in scoped:
                text = _ref_pattern(ref).sub(" ", text)
            text = re.sub(r"\s+", " ", text).strip()
            # 전부 문서명이라 비었으면 원본 유지(빈 쿼리는 0건/오검색 위험).
            if not text:
                text = q.query_text
        # references 는 최종 query_text 에 실제 남은 ref 만(스코프로 제거된 ref 는 제외).
        present_refs = tuple(
            r for r in refs if r and r.lower() in text.lower()
        )
        out.append(FormulatedQuery(
            slot_name=q.slot_name, query_text=text,
            target=q.target, filters=q.filters, references=present_refs,
            scope_audit=q.scope_audit,
        ))
    return tuple(out)
