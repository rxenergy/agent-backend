from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

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
    # NRC nrc-all-v1 스키마 전용 필드 (선택, 다운스트림 인용/필터링용)
    collection: str | None = None
    search_type: str | None = None  # "manual" | "nuscale"
    source_id: str | None = None  # ADAMS Accession Number 또는 govinfo packageId
    page_end: int | None = None
    title: str | None = None  # doc_metadata.DocumentTitle 또는 doc_metadata.title


class RetrieverSearchInput(BaseModel):
    """v2 §8 — `retriever.search` 입력 스키마. 모든 adapter가 이 모델만 받는다."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    top_k: int = 3
    scenario_object: str | None = None
    scenario_depth: str | None = None
    entities: dict[str, list[str]] = Field(default_factory=dict)


class RetrieverSearchOutput(BaseModel):
    """v2 §8 — `retriever.search` 출력 스키마. ToolResult.output에 dump되어 실린다."""

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
    fusion: str = "rrf"
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
