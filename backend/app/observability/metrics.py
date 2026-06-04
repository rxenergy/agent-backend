"""OTLP 메트릭 계측 — plan §6 노드별 집계 신호.

경로 B(otel.install_metrics): instrument 는 모듈 import 시 proxy meter 로 생성되고,
`MeterProvider` 설치 후 첫 측정에서 실제 meter 로 lazily resolve 된다. provider 가
없으면(=유닛 테스트) 측정은 silent no-op 이므로 테스트는 provider 없이 통과한다.

카디널리티 규율(plan §7.1 — 비협상): 라벨은 *bounded enum* 만. id·hash·자유텍스트
(interaction_id·trace_id·*_hash·chunk_id·query_text·entity 값)는 절대 라벨이 아니다 —
그건 trace/log/event 의 몫. 고카디널리티 라벨 하나가 collector 를 붕괴시킨다.
"""

from __future__ import annotations

from typing import Any

from app.observability.otel import get_meter

# scenario_object/depth 는 active-cell 격자라 유한(is_active) → 라벨 안전.
# 그 외 라벨은 모두 enum(decision/status/reason/mode/action/strategy/tool/outcome).


def _attrs(**kwargs: Any) -> dict[str, Any]:
    """None 라벨 값을 'unknown' 으로 치환(OTel attribute 는 None 불가). 빈 문자열도
    'unknown' 으로 — 빈 라벨은 Prometheus 에서 추적 불가."""
    return {k: (v if v not in (None, "") else "unknown") for k, v in kwargs.items()}


class AgentMetrics:
    """conductor 가 노드 경계에서 호출하는 얇은 tap 집합(plan §8 레버 3 — 도메인
    밖, OTel SDK 는 이 레이어에만). 모든 record_* 는 1줄 위임."""

    def __init__(self) -> None:
        m = get_meter("agent")

        # --- 터미널(요청 1건의 최종 귀결) ---
        self._requests = m.create_counter(
            "agent.requests", description="terminal agent runs by outcome"
        )
        # unit 미지정 — OTLP→Prometheus 변환의 unit 접미사 모호성을 피해 메트릭명을
        # 예측가능하게(agent_request_latency_*). 값은 ms 스케일(record_terminal).
        self._request_latency = m.create_histogram(
            "agent.request.latency", description="end-to-end run latency (ms)"
        )
        self._refusals = m.create_counter(
            "agent.refusals", description="refusals by reason (smooth 금지 §6)"
        )

        # --- Node 1 분류 ---
        self._classification_confidence = m.create_histogram(
            "agent.classification.confidence",
            description="classifier confidence per (object,depth)",
        )

        # --- Node 4 scope / Node 5 검색 ---
        self._scope_mode = m.create_counter(
            "agent.scope.mode", description="corpus_map scope mode (recall 절벽 Q5)"
        )
        self._retrieval_pool = m.create_histogram(
            "agent.retrieval.pool_size", description="fused candidate pool size"
        )
        self._strategy_failures = m.create_counter(
            "agent.strategy.failures", description="per-strategy retrieval failures"
        )

        # --- Node 6 게이트 / Node 7 복구 (Q1) ---
        self._gate_decision = m.create_counter(
            "agent.gate.decision", description="retrieval-eval gate decision"
        )
        self._recover_rounds = m.create_histogram(
            "agent.recover.rounds", description="recover rounds spent per run"
        )
        self._recover_outcome = m.create_counter(
            "agent.recover.outcome", description="gate decision after recover (WEAK→PASS flip)"
        )

        # --- Node 8 다홉 / P1a 병합 / P1b 예산 ---
        self._hops = m.create_histogram(
            "agent.hops", description="multi-hop edges followed"
        )
        self._section_merge = m.create_counter(
            "agent.section_merge", description="runs where section auto-merge fired"
        )
        self._budget_actions = m.create_counter(
            "agent.budget.actions", description="context-budget actions by kind"
        )

        # --- Node 10 메모리 / Node 12 advisory ---
        self._memory_inject = m.create_counter(
            "agent.memory.inject", description="session-memory inject decision"
        )
        self._quality_advisory = m.create_counter(
            "agent.quality_advisory", description="runs carrying a WEAK retrieval advisory"
        )

        # --- Node 13 생성 ---
        self._tokens = m.create_histogram(
            "agent.tokens", description="token usage by kind(prompt/completion)"
        )

        # --- Node 14 분해 / Node 15 검증 (Q3) ---
        self._claims = m.create_histogram(
            "agent.claims", description="claims decomposed per answer"
        )
        self._verification_status = m.create_counter(
            "agent.verification.status", description="aggregate claim-verification status"
        )
        self._faithfulness = m.create_histogram(
            "agent.faithfulness", description="supported-claim fraction (judge-dependent §10)"
        )
        self._contradicted = m.create_counter(
            "agent.contradicted", description="runs with ≥1 contradicted claim"
        )

        # --- ToolExecutor ---
        self._tool_calls = m.create_counter(
            "agent.tool.calls", description="tool invocations by (tool,status)"
        )
        self._tool_retries = m.create_counter(
            "agent.tool.retries", description="tool retry attempts"
        )

    # ---- 터미널 ----
    def record_terminal(self, *, outcome: str, latency_ms: float,
                        scenario_object: str | None, scenario_depth: str | None) -> None:
        a = _attrs(outcome=outcome, scenario_object=scenario_object,
                   scenario_depth=scenario_depth)
        self._requests.add(1, a)
        self._request_latency.record(latency_ms, a)

    def record_refusal(self, *, reason: str | None) -> None:
        self._refusals.add(1, _attrs(reason=reason))

    # ---- 노드 ----
    def record_classification(self, *, confidence: float,
                              scenario_object: str | None, scenario_depth: str | None) -> None:
        self._classification_confidence.record(
            confidence, _attrs(scenario_object=scenario_object, scenario_depth=scenario_depth)
        )

    def record_scope(self, *, mode: str | None) -> None:
        self._scope_mode.add(1, _attrs(mode=mode))

    def record_retrieval(self, *, pool_size: int, failed_strategies: list[str]) -> None:
        self._retrieval_pool.record(pool_size)
        for st in failed_strategies or []:
            self._strategy_failures.add(1, _attrs(strategy=st))

    def record_gate(self, *, decision: str | None) -> None:
        self._gate_decision.add(1, _attrs(decision=decision))

    def record_recover(self, *, rounds: int, outcome: str | None) -> None:
        self._recover_rounds.record(rounds)
        if rounds > 0:
            self._recover_outcome.add(1, _attrs(outcome=outcome))

    def record_hops(self, *, num_edges: int) -> None:
        self._hops.record(num_edges)

    def record_section_merge(self, *, merged: int) -> None:
        if merged > 0:
            self._section_merge.add(1)

    def record_budget_actions(self, *, actions: list[str]) -> None:
        for act in actions or []:
            # "demote:cid" / "drop:cid" / "reorder:litm" → 종류(접두)만 라벨로.
            kind = act.split(":", 1)[0]
            self._budget_actions.add(1, _attrs(action=kind))

    def record_memory_inject(self, *, inject: bool) -> None:
        self._memory_inject.add(1, _attrs(decision="inject" if inject else "skip"))

    def record_quality_advisory(self) -> None:
        self._quality_advisory.add(1)

    def record_tokens(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        self._tokens.record(prompt_tokens, _attrs(kind="prompt"))
        self._tokens.record(completion_tokens, _attrs(kind="completion"))

    def record_decompose(self, *, num_claims: int) -> None:
        self._claims.record(num_claims)

    def record_verification(self, *, status: str | None, faithfulness: float,
                            contradicted: bool) -> None:
        self._verification_status.add(1, _attrs(status=status))
        self._faithfulness.record(faithfulness)
        if contradicted:
            self._contradicted.add(1)

    def record_tool(self, *, tool: str, status: str, retry_count: int) -> None:
        self._tool_calls.add(1, _attrs(tool=tool, status=status))
        if retry_count:
            self._tool_retries.add(retry_count, _attrs(tool=tool))


_METRICS: AgentMetrics | None = None


def get_metrics() -> AgentMetrics:
    """프로세스 단일 인스턴스. instrument 는 proxy meter 위에 한 번만 생성하고,
    provider 설치 후 lazily resolve 시킨다."""
    global _METRICS
    if _METRICS is None:
        _METRICS = AgentMetrics()
    return _METRICS
