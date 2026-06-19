from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RetrievedChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    score: float
    page: int | None = None
    section: str | None = None
    snippet: str | None = None
    text: str | None = None  # full text only when CONTEXT_CAPTURE_MODE=full
    doc_type: str | None = None  # NRC 도메인: collection 값 (10CFR/DSRS/FR/RG/SRP/nuscale_*)
    revision: str | None = None
    response_date: str | None = None  # ADAMS DocumentDate 또는 govinfo dateIssued
    # v3.1 규제 구조 메타 (Node 6 G3 Regulatory Structural 신호의 입력).
    # 인덱스 _source 에 존재하면 그대로, 없으면 어댑터가 collection 등에서 유도.
    clause_id: str | None = None  # 조문 ID (예: RG_1_157, 10CFR50.46) — exact-match 게이트용
    authority_tier: str | None = None  # primary > secondary > tertiary (authority_tier ≥ secondary hard gate)
    jurisdiction: str | None = None  # KINS | NRC | IAEA
    effective_on: str | None = None  # 발효/개정 기준일 — version_match 게이트용 (YYYY-MM-DD)
    # NRC nrc-all-v1 스키마 전용 필드 (선택, 다운스트림 인용/필터링용)
    collection: str | None = None
    search_type: str | None = None  # "manual" | "nuscale"
    source_id: str | None = None  # ADAMS Accession Number 또는 govinfo packageId
    page_end: int | None = None
    title: str | None = None  # doc_metadata.DocumentTitle 또는 doc_metadata.title
    # v3.1 Section auto-merge(P1) join 키 — 인덱스 section_path(keyword 배열) 원본.
    # `section`(문자열)은 표시용이고 section_path_str 은 analyzed text 라 exact-match
    # 불가하므로, 형제 fetch 는 이 배열의 최말단 원소를 term 필터로 쓴다.
    section_path: list[str] | None = None
    # v3.1 노이즈 floor(Layer 2) 입력 — chunk 본문 토큰 수. 인덱스 _source 의
    # token_count 를 그대로 싣는다(목차·헤더·단어 fragment 는 작다). local 어댑터는
    # snippet 단어수로 근사. min_token_count 필터/포스트필터가 이 값을 읽는다.
    token_count: int | None = None
    # 인덱싱 단계에서 본문에서 분리된 표. 본문(text/snippet)에는 [TABLE: <tag>]
    # 마커가 남고, 표 실제 내용은 tables 배열의 매칭 엔트리(tag/caption/markdown/html)
    # 에 위치한다. OpenSearch _source.tables(array, object enabled:false) 원본 그대로 —
    # ContextBuilder 가 생성 프롬프트 렌더 시 마커를 caption+markdown 으로 인라인
    # 치환한다(spec_driven_table_inline_expansion).
    tables: list[dict[str, Any]] | None = None
    # 검색 스코프 표준 메타(spec_driven_search_scope_metadata) — 회수 결과의 현행성/설계/
    # 버전 묶음을 인용·감사에 노출. doc_metadata.std_* 원본을 그대로 싣는다(없으면 None —
    # 규제 문서는 std_design 빈값, NuScale 은 std_status 빈값이라 한쪽은 항상 None).
    std_status: str | None = None  # current | history | draft | withdrawn | ... (RG/SRP/DSRS)
    std_design: str | None = None  # US_460 | US_600 (nuscale_*)
    std_canonical_id: str | None = None  # 버전 묶음 키(예: RG-1.206) — Rev 미포함
    # 원문 다운로드 URL — 인덱스 doc_metadata 에 이미 존재(ADAMS=Url, govinfo/10CFR=
    # download_pdfLink, fallback detailsLink). References 딥링크의 *1차* 소스. 없으면
    # 호출측(answer_renderer)이 adams_url(ML번호 재구성) → 평문 순으로 강등한다.
    source_url: str | None = None


class RetrieverSearchInput(BaseModel):
    """v2 §8 — `retriever.search` 입력 스키마. 모든 adapter가 이 모델만 받는다."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    top_k: int = 3
    scenario_object: str | None = None
    scenario_depth: str | None = None
    entities: dict[str, list[str]] = Field(default_factory=dict)
    # v3.1 Node 5 다전략 검색. 코드(dispatcher)가 전략별로 한 번씩 호출한다.
    # OpenSearch 어댑터는 이 값을 search_pipeline 선택에 사용(동일 hybrid DSL에
    # weight 변종 pipeline 적용). local 어댑터는 무시. 미지정 시 hybrid.
    strategy: str = "hybrid"
    # v3.1 범위 한정(Layer 1) — corpus_map 이 confidence-게이트로 산출.
    #   target  : boost-scope. {"collection": ["SRP","DSRS"]} → in-scope 문서 가산
    #             (BM25 should boost). 배제하지 않음 → recall-safe.
    #   filters : hard-scope. {"collection": ["10CFR"], "search_type": "manual"} →
    #             hybrid.filter term/terms 로 모집단 제한. 복구 라운드에선 해제됨.
    #   min_token_count : 노이즈 floor(Layer 2). 본문 토큰 < N 인 chunk 제외.
    # 셋 다 빈/0 기본값 → 비울 때 DSL 불변(local retriever 무영향).
    target: dict[str, list[str]] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    min_token_count: int = 0


class RetrieverSearchOutput(BaseModel):
    """v2 §8 — `retriever.search` 출력 스키마. ToolResult.output에 dump되어 실린다."""

    model_config = ConfigDict(frozen=True)

    chunks: list[RetrievedChunk] = Field(default_factory=list)
    # agentic_finder `retrieval.search` 가 reranker 정렬 점수를 chunks 와 *같은 순서*
    # 로 싣는다(FinderRound.reranker_score_dist 계측 입력). 기존 retriever.search·
    # 다운스트림은 이 필드를 안 채우고 안 읽으므로 무영향(기본 빈 리스트).
    rerank_scores: list[float] = Field(default_factory=list)


class RerankInput(BaseModel):
    """v3.1 Node 5 — `retriever.rerank` 입력. 1차 검색(hybrid) 후보 풀을 cross-encoder
    가 질의-문서 쌍으로 재채점한다. dispatcher 가 RRF 대신 이 도구로 *순위*를 정한다.

    `candidates` 는 1차 검색이 올린 후보(중복 제거된 union). reranker 는 후보를
    *재정렬*만 할 뿐 새 문서를 만들지 않는다(1차 recall 이 상한)."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    candidates: list[RetrievedChunk] = Field(default_factory=list)
    top_k: int = 20


class RerankOutput(BaseModel):
    """`retriever.rerank` 출력. `chunks` 의 *순서*가 권위(authoritative rerank rank).
    `RetrievedChunk.score` 는 raw 1차 점수 그대로 — rerank 점수는 `scores` 가 별도로
    싣는다(RRF 시절 rrf_scores 와 동형: 순서=권위, score 는 raw 보존)."""

    model_config = ConfigDict(frozen=True)

    chunks: list[RetrievedChunk] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)  # chunk_id → rerank 점수


class SlotVerdict(str, Enum):
    """verify_slot BINARY 판정 — 슬롯 1차 검색 결과 전체를 두 상태로만 분류한다(3-state 금지).

    HAS_NECESSARY: 청크 식별자(necessary/neighbor/multihop)를 산출 → follow_up 경로(기존).
    NONE_NECESSARY: 검색 결과 전체가 빗나가 쓸 청크가 없음 → 청크 대신 단일 opinion
    (why_not_needed + what_is_needed)을 내고, retrieval.rescope 가 검색 스코프를 재계획해
    1회 재검색한다(3차 홉 없음). 어느 청크라도 도움되면 HAS_NECESSARY 가 우선."""

    HAS_NECESSARY = "has_necessary"
    NONE_NECESSARY = "none_necessary"


class VerifySlotInput(BaseModel):
    """retrieval.verify_slot 입력(spec_driven_v2 Node2 검증). 슬롯 1개의 1차 검색
    결과를 "사용자 질문 + answer_spec + 검색 쿼리" 기준으로 판정한다."""

    model_config = ConfigDict(frozen=True)

    query_text: str               # 사용자 원질의
    answer_spec: str              # 렌더된 답변 사양 블록(_render_spec_block 산출)
    slot_name: str
    slot_query: str               # 이 슬롯의 N2 검색 쿼리(query_text)
    chunks: list[RetrievedChunk]  # 이 슬롯의 1차 검색 결과


class VerifySlotResult(BaseModel):
    """retrieval.verify_slot 출력. 출력은 청크 *식별자*다(address-not-content 불변 —
    내용을 옮기지 않고 어떤 청크가 필요/멀티홉인지만 가리킨다). 두 집합 ⊆ 입력 chunk_id.

    `method`("llm"|"fallback")로 silent degrade 방지 — fallback 은 모델 응답 파싱불가/
    LLM 미가용 시 안전 기본값(necessary=전체, multihop=없음 → 전량 보존·재검색 안 함)."""

    model_config = ConfigDict(frozen=True)

    necessary_chunk_ids: list[str] = Field(default_factory=list)  # 답변에 꼭 필요한 청크
    multihop_chunk_ids: list[str] = Field(default_factory=list)   # 멀티홉(재검색) 필요 청크
    # multihop 청크 chunk_id → search_direction(그 외부 문서를 어느 방향/각도로 재검색할지
    # 1문장). follow_up 이 재검색 쿼리를 만들 때 이 방향을 우선 반영한다. 키 ⊆ multihop_chunk_ids.
    multihop_search_directions: dict[str, str] = Field(default_factory=dict)
    # necessary 청크 중 본문이 잘려 앞/뒤 문맥 보강이 필요한 것 → chunk_id → "before"|"after"|"both".
    # 러너가 이웃 chunk_id 를 계산해 document.fetch_chunks 로 가져온다. 키 ⊆ necessary_chunk_ids.
    neighbor_requests: dict[str, str] = Field(default_factory=dict)
    # LLM 산출이 아니라 러너/어댑터가 구조화 필드(neighbor/multihop 수·fallback 사유)에서
    # 합성한 요약 — 핀/ UI thinking 연속성용(verify_slot_v2 는 더 이상 자유 rationale 을 내지 않음).
    rationale: str = ""
    method: str = "llm"  # "llm" | "fallback"
    # BINARY 판정(SlotVerdict). 기본 has_necessary → verdict 없는 기존 출력은 오늘과 동일.
    verdict: str = SlotVerdict.HAS_NECESSARY.value
    # none_necessary 일 때만 채워진다: 전체 청크 집합이 왜 불필요한지(why_not_needed) +
    # 어떤 종류의 청크/정보가 필요한지(what_is_needed). retrieval.rescope 가 이걸 받아 스코프
    # 를 재계획한다. address-not-content 불변 — 청크 본문 복사가 아니라 필요한 *종류*의 기술.
    opinion: dict[str, str] = Field(default_factory=dict)


class FollowUpQueryItem(BaseModel):
    """개별 재검색 쿼리 — 참조된 외부 문서 내에서 원래 의도를 재검색하기 위한 쿼리."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    target_source_ids: list[str]
    intent: str = ""


class FollowUpInput(BaseModel):
    """retrieval.follow_up 도구 입력.

    `answer_spec`/`slot_query`/`necessity_only` 는 spec_driven_v2 N3.5 고도화용
    (옵셔널 — 미지정 시 v1 동작 byte-identical). `necessity_only=True` 면 모든 외부
    참조를 추출하지 않고 answer_spec+slot_query 기준 "답변에 꼭 필요한" 참조만 선별한다."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    chunks: list[RetrievedChunk]
    min_score: float = 0.6
    answer_spec: str | None = None   # 렌더된 답변 사양 블록(v2). None → v1 동작.
    slot_query: str | None = None    # 이 슬롯의 N2 검색 쿼리(v2). None → v1 동작.
    necessity_only: bool = False     # True → 필요-판정 선별(v2). False → 전체 추출(v1).
    # verify_slot 이 멀티홉 청크별로 산출한 search_direction(chunk_id → 방향 1문장). 추출기가
    # 그 청크의 재검색 쿼리를 만들 때 우선 반영한다. 빈 dict → 기존 동작 byte-identical.
    search_directions: dict[str, str] = Field(default_factory=dict)


class FollowUpResult(BaseModel):
    """retrieval.follow_up 도구 출력."""

    model_config = ConfigDict(frozen=True)

    follow_up_queries: list[FollowUpQueryItem] = Field(default_factory=list)
    ref_filter: dict[str, Any] = Field(default_factory=dict)


class RescopeInput(BaseModel):
    """retrieval.rescope 입력 — none_necessary 슬롯의 검색 스코프 재계획.

    verify_slot 이 "이 슬롯 1차 검색 결과 전체가 불필요"(none_necessary)로 판정하면,
    그 의견(why_not_needed/what_is_needed)과 1차 planning 스코프 요약(initial_scope)을
    받아 planning 단계와 동일한 스코프 어휘(collection/status/design/canonical_id +
    boost/filter mode)로 검색 스코프를 *새로* 잡는다(완전 재계획 — collection 전환 허용,
    단 초기 스코프를 *고려 대상*으로 전달). follow_up(외부 문서 target_source_ids 산출)과
    출력 계약이 달라 별도 도구로 둔다."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    answer_spec: str               # 렌더된 답변 사양 블록(_render_spec_block 산출)
    slot_name: str
    slot_query: str                # 1차 검색에 쓴 슬롯 쿼리(text)
    why_not_needed: str            # verify_slot opinion — 1차 결과가 왜 빗나갔는지
    what_is_needed: str            # verify_slot opinion — 어떤 종류의 청크/정보가 필요한지
    # 1차 planning 스코프 요약(collection/status/design/canonical_id + mode). 재계획 시
    # *고려 대상*(고정 아님) — 빈 dict 면 스코프 힌트 없이 재계획.
    initial_scope: dict[str, Any] = Field(default_factory=dict)
    max_queries: int = 3


class RescopeQueryItem(BaseModel):
    """재계획된 검색 쿼리 1개 — planning(FormulatedQuery)과 동일 표현. query_text +
    결정형 게이트로 해소된 target(boost)/filters(hard-scope). scope_audit 는 무시/기각된
    채널 감사(silent drop 금지)."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    target: dict[str, list[str]] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    scope_audit: dict[str, Any] = Field(default_factory=dict)


class RescopeResult(BaseModel):
    """retrieval.rescope 도구 출력. `method`("llm"|"fallback")로 silent degrade 방지 —
    fallback 은 LLM 미가용/파싱 실패 시 빈 queries(파이프라인이 재검색 skip)."""

    model_config = ConfigDict(frozen=True)

    queries: list[RescopeQueryItem] = Field(default_factory=list)
    method: str = "llm"  # "llm" | "fallback"


class DocumentFetchSectionInput(BaseModel):
    """v3.1 P1 `document.fetch_section` 입력 — 한 Section 의 형제 문단 일괄 fetch.

    relevance 검색이 아니라 메타 표적 조회다: `source_id` + `section_key`(해당
    chunk section_path 의 최말단 원소) 로 같은 Section 의 모든 chunk 를 가져온다.
    Node 8 다홉도 같은 도구로 §N.M 참조 섹션을 fetch 한다."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    section_key: str
    max_chunks: int = 50
    # "term"  = section_path 원소 exact 매칭(P1a — chunk 자신의 full 경로 원소).
    # "prefix"= section_path 원소 prefix 매칭(P2 다홉 — "§3.2" 같은 번호만 알 때
    #            "3.2 Title" 류 원소를 매칭). keyword 필드라 prefix 쿼리 사용.
    match: str = "term"


class DocumentFetchSectionOutput(BaseModel):
    """`document.fetch_section` 출력 — chunk_id ordinal 순 정렬된 형제 chunk."""

    model_config = ConfigDict(frozen=True)

    chunks: list[RetrievedChunk] = Field(default_factory=list)


class DocumentFetchChunksInput(BaseModel):
    """`document.fetch_chunks` 입력 — chunk_id 의 *정확한* 집합을 메타 표적으로 일괄 fetch.

    relevance 검색이 아니라 id 조회다(document.resolve_citation 의 terms 쿼리와 동형).
    verify_slot 의 neighbor_requests(앞/뒤 문맥 보강)에서 러너가 이웃 chunk_id 를
    `..._cNNNN` ordinal 로 계산해 넘긴다. 출력은 RetrieverSearchOutput(chunks) 재사용."""

    model_config = ConfigDict(frozen=True)

    chunk_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# v3.1 (hierarchical_corrective) — Phase B Retrieval reproducibility models.
#
# These are frozen *dataclasses*, not pydantic models. The pydantic models
# above (RetrievedChunk / RetrieverSearchInput / RetrieverSearchOutput) are
# tool-I/O schemas validated at the adapter boundary with `model_validate`.
# The models below instead carry reproducibility state that is embedded into
# `InteractionEvent` and serialized with `dataclasses.asdict()` — which only
# recurses into dataclasses/dict/list/tuple. Keep these two families distinct.
# ---------------------------------------------------------------------------


class GateDecision(str, Enum):
    """Node 6 per-chunk / per-sub_question / overall gate verdict."""

    PASS = "pass"
    WEAK = "weak"
    FAIL = "fail"


@dataclass(frozen=True)
class RetrievalStrategy:
    """One leg of a multi-strategy plan (Node 4)."""

    name: str  # search_bm25 | search_vector | search_hybrid | search_clause_id | ...
    args_hash: str | None = None


@dataclass(frozen=True)
class RetrievalPlan:
    """Node 4 output — rule-selected strategy set + fusion method. `plan_hash`
    = sha256(rule_id || entity_hash), the reproducibility anchor for the plan."""

    rule_id: str
    strategies: tuple[RetrievalStrategy, ...] = ()
    fusion: str = "rerank"  # v3.1 — RRF 제거, cross-encoder reranker 가 순위 권위.
    plan_hash: str | None = None


@dataclass(frozen=True)
class ChunkSignals:
    """Node 6 — per-chunk G1–G5 raw signals + weighted total + gate decision
    (spec §6.2). Every raw value is retained so the gate decision is auditable
    from the event alone. `decision` is a `GateDecision` value."""

    chunk_id: str
    s_lex: float = 0.0
    s_sem: float = 0.0
    s_reg: float = 0.0
    s_ens: float = 0.0
    s_total: float = 0.0
    entity_coverage: float = 0.0
    hard_gates_passed: bool = False
    decision: str = GateDecision.FAIL.value
    # v3.1 P1 — Section auto-merge 승격 대상으로 표시됐는지(decision≠PASS 등).
    # 점수에는 영향 없고 event 기록용: "왜 이 문단을 Section 으로 키웠나".
    promote: bool = False


@dataclass(frozen=True)
class SubQuestionDecision:
    """Node 6 — per-sub_question aggregate over its chunks' decisions."""

    sub_question_id: str
    decision: str  # GateDecision value
    n_pass: int = 0
    n_weak: int = 0
    n_fail: int = 0


@dataclass(frozen=True)
class EvaluationResult:
    """Node 6 full result — per-chunk signals, per-sub_question aggregation,
    overall decision, and the policy hash that pins the weights/thresholds."""

    per_chunk: tuple[ChunkSignals, ...] = ()
    per_sub_question: tuple[SubQuestionDecision, ...] = ()
    overall_decision: str = GateDecision.FAIL.value
    evaluator_policy_hash: str | None = None
    # v3.1 — 규제 hard gate(authority_tier)가 *실제로 강제되었는지*. v1 에서는
    # clause_id/effective_on 부재 + collection-유도 tier 라 false. 이 플래그가
    # false 인 PASS 는 "규제 근거가 검증된 PASS"가 아님을 downstream/감사에
    # 알린다(advisor: unknown 이 verified 로 둔갑하지 않게).
    regulatory_enforced: bool = False


@dataclass(frozen=True)
class RecoverRound:
    """Node 7 — one deterministic recovery attempt (spec §5 Node 7 table)."""

    round_index: int
    diagnosis: str
    recover_strategy_id: str
    triggered_sub_question_ids: tuple[str, ...] = ()
    outcome_decision: str | None = None  # GateDecision after re-eval


@dataclass(frozen=True)
class HopEdge:
    """Node 8(v3.1) / N4(agentic_finder) — one cross-reference hop in the
    multi-hop expansion graph.

    agentic_finder N4(Multi-Hop Sequence + Document Mapper, 현재 stub)는 인용
    문자열을 Document Mapper 로 검색 파라미터로 해소하므로 추가 필드를 *기본값으로*
    싣는다(설계 finder §5). 기본값만 추가하므로 v3.1 의 기존 4-인자 생성은 불변."""

    from_chunk_id: str
    ref_kind: str  # definition | parent_section | clause_id
    target_id: str
    grade: str | None = None  # GateDecision after re-eval of the hopped chunk
    # agentic_finder N4 확장 — 인용→문서 해소(Document Mapper) 추적.
    hop_depth: int = 0
    citation_text: str | None = None  # 추출된 인용 원문(예: "RG 1.157", "§3.2").
    mapper_params: dict[str, Any] | None = None  # Mapper 가 산출한 검색 파라미터.
    resolved: bool = False  # 인용이 코퍼스 문서로 해소됐는가.
    resolved_doc_id: str | None = None


@dataclass(frozen=True)
class EvidenceSnippet:
    """Node 9 — a sentence-window snippet with citation metadata attached."""

    snippet_id: str
    chunk_id: str
    text: str
    citation_id: str | None = None
    document_id: str | None = None
    section: str | None = None
    page: int | None = None
    revision: str | None = None


@dataclass(frozen=True)
class EvidencePack:
    """Node 9 output — the snippet set fed to ContextBuilder. `pack_hash` is
    the reproducibility anchor for the evidence selection."""

    snippets: tuple[EvidenceSnippet, ...] = ()
    pack_hash: str | None = None
    snippet_extractor_version: str | None = None
