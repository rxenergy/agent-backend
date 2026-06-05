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
    # 셋 다 빈/0 기본값 → 비울 때 DSL 불변(sequential_tool_routed_v2·local 무영향).
    target: dict[str, list[str]] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    min_token_count: int = 0


class RetrieverSearchOutput(BaseModel):
    """v2 §8 — `retriever.search` 출력 스키마. ToolResult.output에 dump되어 실린다."""

    model_config = ConfigDict(frozen=True)

    chunks: list[RetrievedChunk] = Field(default_factory=list)


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
    """Node 8 — one cross-reference hop in the multi-hop expansion graph."""

    from_chunk_id: str
    ref_kind: str  # definition | parent_section | clause_id
    target_id: str
    grade: str | None = None  # GateDecision after re-eval of the hopped chunk


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
