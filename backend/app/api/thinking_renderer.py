"""Translate AgentEvents into human-readable reasoning lines.

Surfaced through OpenAI-compatible `delta.reasoning_content` (streaming) or
`<think>…</think>` content prefix (non-streaming) so that OpenWebUI renders
agent workflow progress in its collapsible thinking block.

Two narration tiers, selected per call by `verbosity` (settings.thinking_verbosity):
  • "summary"  — user-meaningful, Korean, outcome-conditioned (default). The
                 16-node v3.1 workflow is distilled to a few load-bearing
                 moments (이해 / 검색·근거 / 근거품질·복구 / 작성 / 검증·한계);
                 internal mechanics and identifiers are dropped (they live on
                 the `smr_agent.event` sidechannel + OTel spans). Modeled after
                 OpenAI's reasoning-*summary* convention — distill, don't dump.
  • "detailed" — legacy per-node English narration (dev/debug); narrates every
                 step. v2/fake_echo always use their single English table.
Domain proper nouns (RG 1.206, KEPIC, KINS-RG) stay in their source form.
Design reference: docs/references/thinking_output_design.md.

The renderer narrates **deterministic workflow steps** (`step`/`tool` events).
The generation LLM's own chain-of-thought rides a *separate* channel
(`reasoning` events → `delta.reasoning_content`) and is not rendered here — the
two surfaces interleave into one thinking block at the API layer. The final
assistant answer (`response.answer_text`) is unaffected.

Each agent variant emits a different step vocabulary, so narration is
dispatched per `variant_id` over shared step handlers (CLAUDE.md principle 1 —
the renderer, not the workflow, owns presentation). v2 and v3.1 share the
handlers for steps with identical semantics (intent_classification,
context_build, prompt_render, generation, …); each contributes its own
handlers for steps unique to its workflow.
"""
from __future__ import annotations

from typing import Callable, Iterable, Literal

from app.application.agents.events import AgentEvent

ContentMode = Literal["metadata", "snippets", "full"]

# A step handler: (status, payload, *, content_mode, max_items) -> lines.
StepHandler = Callable[..., list[str]]


Verbosity = Literal["summary", "detailed", "off"]


def render(
    event: AgentEvent,
    *,
    variant_id: str | None = None,
    content_mode: ContentMode = "metadata",
    max_items: int = 3,
    verbosity: Verbosity = "summary",
) -> list[str]:
    """Return zero or more thinking lines for an event.

    Pure function — no side effects, no I/O. Empty list means "drop this
    event" (no thinking output). Each returned string is one logical line
    (callers add a trailing newline).

    `verbosity` selects the narration tier:
      • "summary"  — user-meaningful, outcome-conditioned, Korean (default).
                     Distilled phase/result lines (OpenAI reasoning-summary
                     model), internal mechanics dropped. See thinking_output
                     design reference.
      • "detailed" — the legacy per-node English narration (dev/debug). Every
                     workflow node is narrated.
      • "off"      — no workflow narration at all.

    `variant_id` selects the step-handler table; when None/unknown the union of
    all variants' handlers is used so callers without variant context still get
    sensible output.
    """
    if verbosity == "off":
        return []
    if event.kind == "step":
        table = _select_table(variant_id or "", verbosity)
        handler = table.get(event.name or "")
        if handler is None:
            return []
        return handler(
            event.status or "", event.payload or {},
            content_mode=content_mode, max_items=max_items,
        )
    if event.kind == "tool":
        return _render_tool(event)
    return []


def _select_table(variant_id: str, verbosity: Verbosity) -> dict[str, StepHandler]:
    """Resolve the (variant, verbosity) → handler table. Only v3.1 has a
    distinct summary tier; other variants share one table across tiers."""
    if verbosity == "detailed":
        return _RENDERERS_DETAILED.get(variant_id, _RENDERERS.get(variant_id, _DEFAULT_STEPS))
    return _RENDERERS.get(variant_id, _DEFAULT_STEPS)


# --- shared step handlers (identical narration across variants) -----------


def _h_intent_classification(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        lines = ["Classifying the user's intent and scoping the question."]
        q = p.get("query")
        if q:
            lines.append(f"  query: {_q(q)}")
        return lines
    if status == "ok":
        so = p.get("scenario_object") or "?"
        sd = p.get("scenario_depth") or "?"
        conf = p.get("confidence")
        conf_s = f" (confidence {conf:.2f})" if isinstance(conf, (int, float)) else ""
        lines = [f"Identified scenario {so} at depth {sd}{conf_s}."]
        ents = p.get("entities") or {}
        if isinstance(ents, dict) and ents:
            entity_line = _fmt_entities(ents)
            if entity_line:
                lines.append(f"  entities: {entity_line}")
        return lines
    return []


def _h_memory_approved_search(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Searching approved memory from prior expert-reviewed answers."]
    if status == "ok":
        n = p.get("hit_count", 0)
        if n == 0:
            return ["No approved memory matched this scenario."]
        lines = [f"Matched {n} approved memory item(s) from prior expert-reviewed answers."]
        lines.extend(_fmt_memory_hits(p.get("hits_preview") or [], max_items))
        return lines
    return []


def _h_context_build(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Assembling the context pack — passages, citations, and memory."]
    if status == "ok":
        return ["Context pack assembled."]
    return []


def _h_prompt_render(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Rendering the prompt template for this scenario/depth profile."]
    if status == "ok":
        pid = p.get("profile_id")
        ver = p.get("profile_version")
        if pid and ver:
            return [f"Prompt rendered (profile {pid}@{ver})."]
        return ["Prompt rendered."]
    return []


def _h_generation(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        llm = p.get("llm_id")
        return [f"Drafting the answer with {llm}." if llm else "Drafting the answer now."]
    if status == "ok":
        return []  # token stream itself carries the visible progress
    return []


# --- v2-only step handlers ------------------------------------------------


def _h_session_memory_load(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Checking prior session context."]
    if status == "ok":
        if p.get("injected"):
            lines = ["Loaded prior session — same scenario continues, injecting it."]
            prior_so = p.get("prior_scenario_object")
            prior_sd = p.get("prior_scenario_depth")
            if prior_so or prior_sd:
                lines.append(f"  prior: {prior_so or '?'} / {prior_sd or '?'}")
            summary = p.get("summary_preview")
            if summary:
                lines.append(f"  summary: {_q(summary)}")
            return lines
        if p.get("present"):
            return ["Prior session found but scenario differs; skipping injection."]
        return ["No prior session context for this conversation."]
    return []


def _h_retrieval(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        lines = ["Searching the regulatory document corpus for relevant passages."]
        q = p.get("query")
        if q:
            lines.append(f"  query: {_q(q)}")
        return lines
    if status == "ok":
        n = p.get("num_chunks", 0)
        if n == 0:
            return ["No matching passages found — I'll need to refuse rather than guess."]
        lines = [f"Retrieved {n} candidate passage(s); ranking by scenario fit:"]
        lines.extend(
            _fmt_chunks(
                p.get("chunks_preview") or [],
                max_items=max_items,
                content_mode=content_mode,
            )
        )
        return lines
    return []


def _h_citation_resolve(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Resolving citations to source documents."]
    if status == "ok":
        n = p.get("resolved_count", 0)
        total = p.get("total", n)
        lines = [f"Resolved {n} of {total} citation(s) to source documents:"]
        lines.extend(_fmt_resolved(p.get("resolved_preview") or [], max_items))
        return lines
    return []


def _h_verification(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Verifying the draft against retrieved evidence."]
    if status == "ok":
        vs = p.get("verification_status") or ""
        cc = p.get("citation_completeness")
        fa = p.get("faithfulness")
        metrics = []
        if isinstance(cc, (int, float)):
            metrics.append(f"citation {cc:.2f}")
        if isinstance(fa, (int, float)):
            metrics.append(f"faithfulness {fa:.2f}")
        mtxt = f" ({', '.join(metrics)})" if metrics else ""
        if vs == "PASS":
            return [f"Verification passed{mtxt}."]
        if vs == "PARTIAL":
            return [f"Verification partial{mtxt} — flagging uncertainty in the answer."]
        if vs == "FAIL":
            return [f"Verification failed{mtxt} — the draft isn't grounded in the evidence."]
        if vs == "SKIPPED":
            return ["Verification skipped."]
    return []


# --- v3.1-only step handlers ----------------------------------------------


def _h_query_understanding(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Parsing the query for sub-questions and version constraints."]
    if status == "ok":
        subq = p.get("sub_questions", 0)
        multi = " (multi-intent)" if p.get("multi_intent") else ""
        return [f"Parsed query: {subq} sub-question(s){multi}."]
    return []


def _h_retrieval_plan(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Planning a retrieval strategy from scenario rules."]
    if status == "ok":
        rule = p.get("rule_id") or "?"
        strategies = p.get("strategies") or []
        s_txt = ", ".join(str(s) for s in strategies) if strategies else "—"
        return [f"Retrieval plan {rule}: {s_txt}."]
    return []


def _h_retrieval_execute(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        strategies = p.get("strategies") or []
        if strategies:
            return [f"Searching the corpus across {', '.join(str(s) for s in strategies)}."]
        return ["Searching the corpus."]
    if status == "ok":
        n = p.get("num_chunks", 0)
        pool = p.get("pool_size", n)
        line = f"Retrieved {n} of {pool} fused passage(s)."
        failed = p.get("strategies_failed") or []
        if failed:
            line = line[:-1] + f"; failed strategies: {', '.join(str(s) for s in failed)}."
        return [line]
    return []


def _h_retrieval_evaluate(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Evaluating retrieval quality against the 5-signal gate."]
    if status == "ok":
        overall = p.get("overall") or "?"
        num_pass = p.get("num_pass", 0)
        reg = ", regulatory gates enforced" if p.get("regulatory_enforced") else ""
        return [f"Gate decision {overall} — {num_pass} passage(s) passed{reg}."]
    return []


def _h_retrieval_recover(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        diagnosis = p.get("diagnosis") or "weak evidence"
        strategy = p.get("strategy") or "recovery"
        rnd = p.get("round")
        rnd_s = f" (round {rnd})" if rnd is not None else ""
        return [f"Retrieval is {diagnosis}; recovering via {strategy}{rnd_s}."]
    if status == "ok":
        rnd = p.get("round")
        outcome = p.get("outcome") or "?"
        rnd_s = f"Recovery round {rnd}" if rnd is not None else "Recovery"
        return [f"{rnd_s} → {outcome}."]
    if status == "skipped":
        return ["Retrieval gate passed; no recovery needed."]
    return []


def _h_multi_hop_expand(status, p, *, content_mode, max_items) -> list[str]:
    # Narrate only when expansion actually happens; a skipped hop is a no-op
    # that adds noise to the trace (research §6 — "summarize, don't dump").
    if status == "started":
        return ["Following cross-references to expand the evidence."]
    if status == "ok":
        hops = p.get("num_hops", p.get("hops"))
        if isinstance(hops, int) and hops:
            return [f"Expanded evidence across {hops} hop(s)."]
        return ["Expanded the evidence with referenced passages."]
    return []


def _h_evidence_snippet(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Extracting evidence sentence windows from the passages."]
    if status == "ok":
        n = p.get("num_snippets", 0)
        return [f"Extracted {n} evidence window(s)."]
    return []


def _h_memory_inject(status, p, *, content_mode, max_items) -> list[str]:
    # Only narrate when memory is actually injected — the decision step is a
    # no-op on most turns and the "started" line is pure transparency.
    if status == "ok" and p.get("inject") and p.get("num_memory_refs"):
        return [f"Injected {p.get('num_memory_refs')} memory item(s)."]
    return []


def _h_claim_decompose(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Decomposing the draft into atomic claims."]
    if status == "ok":
        n = p.get("num_claims", 0)
        method = p.get("method") or "?"
        return [f"Decomposed the draft into {n} claim(s) via {method}."]
    return []


def _h_claim_verify(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["Verifying each claim against the cited evidence."]
    if status == "ok":
        vs = p.get("verification_status") or "?"
        n = p.get("num_claims", 0)
        extra = []
        if p.get("contradicted"):
            extra.append("contradicted claim(s) found")
        if p.get("entailment_ran"):
            extra.append("entailment run")
        etxt = f" ({', '.join(extra)})" if extra else ""
        return [f"Verification {vs} — {n} claim(s){etxt}."]
    return []


def _h_selective_regenerate(status, p, *, content_mode, max_items) -> list[str]:
    # Skipped regeneration is the common case (a no-op) — narrate only the work.
    if status == "started":
        return ["Selectively regenerating unsupported claims."]
    if status == "ok":
        n = p.get("num_regenerated", p.get("regenerated"))
        if isinstance(n, int) and n:
            return [f"Regenerated {n} unsupported claim(s)."]
        return ["Regenerated the unsupported portions of the answer."]
    return []


# --- v3.1 summary tier (Korean, outcome-conditioned) ----------------------
# Design: docs/references/thinking_output_design.md. The summary tier distills
# the 16-node workflow into a few user-meaningful moments (이해 / 검색·근거 /
# 근거품질·복구 / 작성 / 검증·한계). Internal mechanics (plan/evidence_snippet/
# context_build/prompt_render/claim_decompose, hashes, gate verdicts) are
# dropped here and live only on the `smr_agent.event` sidechannel + OTel spans.
# Domain proper nouns (RG 1.206, KEPIC, KINS-RG) stay in their source form.

# scenario_object / scenario_depth 코드 → 사용자용 한국어 라벨(표현 계층 소유 —
# CLAUDE.md 원칙 1). 의미는 domain/scenario.py 주석에 정의됨.
_OBJECT_LABEL_KO = {
    "O1": "공급사·노형 설계",
    "O2": "규제 요건",
    "O3": "RAI(추가정보요청)",
    "O4": "규제-설계 연계",
}
_DEPTH_LABEL_KO = {
    "D1": "개요",
    "D2": "기술 상세",
    "D3": "조문 수준",
}


def _scenario_label_ko(so: str | None, sd: str | None) -> str:
    obj = _OBJECT_LABEL_KO.get(so or "", so or "?")
    depth = _DEPTH_LABEL_KO.get(sd or "", sd or "?")
    return f"{obj} · {depth}"


def _h31_intent(status, p, *, content_mode, max_items) -> list[str]:
    if status != "ok":
        return []
    label = _scenario_label_ko(p.get("scenario_object"), p.get("scenario_depth"))
    line = f"질문을 〈{label}〉(으)로 이해했습니다."
    ents = p.get("entities") or {}
    if isinstance(ents, dict) and ents:
        ent_line = _fmt_entities(ents)
        if ent_line:
            line += f" 핵심어: {ent_line}."
    return [line]


def _h31_query_understanding(status, p, *, content_mode, max_items) -> list[str]:
    # 단일 의도(거의 항상)는 기계 동작 — 다중 의도이거나 하위 질의가 2개 이상일
    # 때만 서술(sub_questions≤1 은 분해가 일어나지 않은 평범한 케이스).
    subq = p.get("sub_questions", 0) or 0
    if status == "ok" and (p.get("multi_intent") or subq > 1):
        return [f"질문에 {subq}개 하위 질의가 있어 나눠 처리합니다."]
    return []


def _h31_retrieval_execute(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["규제 문서에서 근거를 검색하는 중…"]
    if status == "ok":
        n = p.get("num_chunks", 0)
        if n == 0:
            return ["관련 근거를 찾지 못했습니다."]
        docs = _fmt_doc_refs(p.get("chunks_preview") or [], max_items)
        if docs:
            return [f"근거 {n}건 확보: {docs}."]
        return [f"근거 {n}건을 확보했습니다."]
    return []


def _h31_retrieval_evaluate(status, p, *, content_mode, max_items) -> list[str]:
    # PASS 는 검색 줄에 흡수(무신호 억제). WEAK/FAIL 만 사유와 함께 노출.
    if status == "ok" and p.get("overall") not in (None, "PASS"):
        reason = p.get("diagnosis_reason")
        if reason:
            return [f"확보한 근거가 부분적입니다 — {reason}"]
        return ["확보한 근거가 부분적입니다."]
    return []


_RECOVER_STRATEGY_KO = {
    "synonym_expand": "동의어 확장",
    "relax_filter": "필터 완화",
}


def _h31_retrieval_recover(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        strat = _RECOVER_STRATEGY_KO.get(p.get("strategy") or "", "근거 확장")
        rnd = p.get("round")
        rnd_s = f" (시도 {rnd + 1})" if isinstance(rnd, int) else ""
        return [f"근거가 부족해 {strat}(으)로 재검색합니다{rnd_s}."]
    return []  # ok/skipped 는 후속 검색·평가 줄로 드러남(중복 억제)


def _h31_multi_hop(status, p, *, content_mode, max_items) -> list[str]:
    if status == "ok":
        hops = p.get("num_hops", 0)
        if isinstance(hops, int) and hops:
            return [f"교차 참조된 조항을 {hops}건 추가로 확인했습니다."]
    return []


def _h31_memory_inject(status, p, *, content_mode, max_items) -> list[str]:
    if status == "ok" and p.get("inject") and p.get("num_memory_refs"):
        return [f"이전 전문가 검토 답변 {p.get('num_memory_refs')}건을 참고합니다."]
    return []


def _h31_generation(status, p, *, content_mode, max_items) -> list[str]:
    if status == "started":
        return ["답변을 작성하는 중…"]
    return []  # 본문 토큰 스트림이 가시적 진행을 담당.


# claim_verify(검증·한계 모먼트)는 summary tier 에서 thinking 에 싣지 않는다.
# 검증 narration 은 generation-verification coupling 상 본문 토큰(content) *이후*
# reasoning_content 로 방출되어 OpenWebUI Thought 블록 조기 종결(#24295)에 걸려
# 신뢰성 있게 렌더되지 않는다. 규제 미검증 한계·검증 결과는 답변 본문(answer_text)에
# durable 고지로 이미 실리므로(CLAUDE.md §6) 이를 backstop 으로 삼고 summary 노드에서
# 드롭한다. detailed tier(dev/debug)는 _h_claim_verify 로 계속 서술한다.
# (thinking_output_design.md 의 claim_verify 행·D3·예시 참조.)


def _h31_selective_regenerate(status, p, *, content_mode, max_items) -> list[str]:
    if status == "ok":
        n = p.get("num_regenerated", p.get("regenerated"))
        if isinstance(n, int) and n:
            return ["근거 불일치 부분을 다시 작성했습니다."]
    return []


_REFUSAL_THINKING_KO = {
    "clarification_required": "질문이 모호해 명확화가 필요합니다 — 답변 대신 되묻습니다.",
    "retrieval_no_result": "관련 근거를 찾지 못했습니다 — 답변 대신 거부합니다.",
    "insufficient_evidence": "복구를 시도했으나 답변 자격을 충족하는 근거를 확보하지 못했습니다 — 답변 대신 거부합니다.",
    "verification_failed": "인용 가능한 근거가 부족해 검증을 통과하지 못했습니다 — 답변 대신 거부합니다.",
    "budget_exceeded": "처리 예산을 초과해 답변을 완료하지 못했습니다 — 거부합니다.",
    "unsupported_scenario": "현재 단계에서 지원되지 않는 유형입니다 — 답변 대신 거부합니다.",
    "unknown_scenario": "지원되지 않는 시나리오 조합입니다 — 답변 대신 거부합니다.",
    "partial_answer": "일부 주장의 근거가 검증을 충족하지 못해 부분 답변으로 표기합니다.",
    "llm_unavailable": "모델을 가져올 수 없어 답변하지 못했습니다 — 잠시 후 재시도가 필요합니다.",
}


def _h31_refused(status, p, *, content_mode, max_items) -> list[str]:
    reason = p.get("reason") or ""
    line = _REFUSAL_THINKING_KO.get(reason)
    return [line] if line else ["근거가 부족하여 답변을 제공하지 못했습니다."]


# --- per-variant dispatch tables ------------------------------------------

_V2_STEPS: dict[str, StepHandler] = {
    "intent_classification": _h_intent_classification,
    "session_memory_load": _h_session_memory_load,
    "memory_approved_search": _h_memory_approved_search,
    "retrieval": _h_retrieval,
    "context_build": _h_context_build,
    "prompt_render": _h_prompt_render,
    "generation": _h_generation,
    "citation_resolve": _h_citation_resolve,
    "verification": _h_verification,
}

# v3.1 detailed tier — the legacy per-node English narration (dev/debug).
# Every node is narrated; this is what `verbosity="detailed"` selects.
_V3_1_DETAILED: dict[str, StepHandler] = {
    "intent_classification": _h_intent_classification,   # shared
    # scenario_routing / multi_hop_expand(skipped) / selective_regenerate(skipped)
    # / memory_inject(no-op) are transparent steps — intentionally not narrated
    # (research §6: surface load-bearing reasoning, not mechanical filler).
    "query_understanding": _h_query_understanding,
    "retrieval_plan": _h_retrieval_plan,
    "retrieval_execute": _h_retrieval_execute,
    "retrieval_evaluate": _h_retrieval_evaluate,
    "retrieval_recover": _h_retrieval_recover,
    "multi_hop_expand": _h_multi_hop_expand,
    "evidence_snippet": _h_evidence_snippet,
    "memory_approved_search": _h_memory_approved_search,  # shared
    "memory_inject": _h_memory_inject,
    "context_build": _h_context_build,                    # shared
    "prompt_render": _h_prompt_render,                    # shared
    "generation": _h_generation,                          # shared
    "claim_decompose": _h_claim_decompose,
    "claim_verify": _h_claim_verify,
    "selective_regenerate": _h_selective_regenerate,
}

# v3.1 summary tier — user-meaningful, Korean, outcome-conditioned (default).
# Nodes absent here (retrieval_plan / evidence_snippet / context_build /
# prompt_render / claim_decompose / claim_verify / scenario_routing /
# section_merge / context_budget) are intentionally dropped: internal mechanics
# with no reliable user signal. `claim_verify`(검증·한계)는 본문 토큰 이후
# reasoning_content 로 나가 OpenWebUI Thought 블록 조기 종결(#24295)에 걸리고,
# 검증 한계는 answer_text durable 고지가 backstop 이므로 summary 에서 드롭한다.
# `refused` is summary-only (D8 — close the trace on a refusal).
_V3_1_SUMMARY: dict[str, StepHandler] = {
    "intent_classification": _h31_intent,
    "query_understanding": _h31_query_understanding,
    "retrieval_execute": _h31_retrieval_execute,
    "retrieval_evaluate": _h31_retrieval_evaluate,
    "retrieval_recover": _h31_retrieval_recover,
    "multi_hop_expand": _h31_multi_hop,
    "memory_inject": _h31_memory_inject,
    "generation": _h31_generation,
    "selective_regenerate": _h31_selective_regenerate,
    "refused": _h31_refused,
}

# Default tier per variant (summary for v3.1). `verbosity="detailed"` overrides
# via `_RENDERERS_DETAILED`.
_RENDERERS: dict[str, dict[str, StepHandler]] = {
    "sequential_tool_routed_v2": _V2_STEPS,
    "hierarchical_corrective_v3_1": _V3_1_SUMMARY,
    "fake_echo_v0": _V2_STEPS,  # minimal; fake echo emits v2-style step names
}

_RENDERERS_DETAILED: dict[str, dict[str, StepHandler]] = {
    "hierarchical_corrective_v3_1": _V3_1_DETAILED,
}

# Union fallback for callers without variant context. v2 and v3.1 step names
# are mostly disjoint; v3-only names use the summary tier, while the few shared
# names (intent_classification / context_build / prompt_render / generation /
# memory_approved_search) resolve to the v2 English handler so the variant-less
# default stays backward-compatible. In production openai_compat always passes
# variant_id, so this union is a fallback only.
_DEFAULT_STEPS: dict[str, StepHandler] = {**_V3_1_SUMMARY, **_V2_STEPS}


def _render_tool(event: AgentEvent) -> list[str]:
    # Successful tool calls are already summarized by the surrounding step
    # events. Only surface failures — they explain why a refusal or fallback
    # is about to happen.
    if event.status == "error":
        code = (event.payload or {}).get("error_code")
        name = event.name or "tool"
        if code:
            return [f"Tool `{name}` failed ({code}); recovering."]
        return [f"Tool `{name}` failed; recovering."]
    return []


# --- formatting helpers ---------------------------------------------------


def _fmt_chunks(
    chunks: Iterable[dict], *, max_items: int, content_mode: ContentMode
) -> list[str]:
    out: list[str] = []
    chunks = list(chunks)
    for i, c in enumerate(chunks[:max_items], start=1):
        title = c.get("title") or c.get("document_id") or c.get("chunk_id") or "?"
        page = c.get("page")
        score = c.get("score")
        doc_type = c.get("doc_type")
        bits = [f"{i}. {title}"]
        if doc_type:
            bits[-1] = f"{i}. [{doc_type}] {title}"
        if page is not None:
            bits.append(f"(p. {page})")
        if isinstance(score, (int, float)):
            bits.append(f"· score {score:.2f}")
        out.append("  " + " ".join(bits))
        snippet = c.get("snippet")
        if snippet and content_mode != "metadata":
            limit = 200 if content_mode == "snippets" else 500
            out.append(f"     {_q(snippet[:limit])}")
    remaining = max(0, len(chunks) - max_items)
    if remaining:
        out.append(f"  … {remaining} more")
    return out


def _fmt_doc_refs(chunks: Iterable[dict], max_items: int) -> str:
    """Compact one-line document/clause reference list for the summary tier —
    e.g. "RG 1.206 §C.I.4 · KEPIC-ENB §3.2 외 1건". Prefers section over page;
    falls back to title/document_id when no section. Scores/hashes are omitted
    (D5/D6 — content over verdicts)."""
    chunks = list(chunks)
    refs: list[str] = []
    for c in chunks[:max_items]:
        name = c.get("title") or c.get("document_id") or c.get("chunk_id") or "?"
        section = c.get("section")
        page = c.get("page")
        if section:
            refs.append(f"{name} §{section}")
        elif page is not None:
            refs.append(f"{name} (p.{page})")
        else:
            refs.append(str(name))
    if not refs:
        return ""
    out = " · ".join(refs)
    remaining = max(0, len(chunks) - max_items)
    if remaining:
        out += f" 외 {remaining}건"
    return out


def _fmt_memory_hits(hits: Iterable[dict], max_items: int) -> list[str]:
    out: list[str] = []
    hits = list(hits)
    for i, h in enumerate(hits[:max_items], start=1):
        mid = h.get("memory_id") or "?"
        score = h.get("score")
        if isinstance(score, (int, float)):
            out.append(f"  {i}. {mid} · score {score:.2f}")
        else:
            out.append(f"  {i}. {mid}")
    remaining = max(0, len(hits) - max_items)
    if remaining:
        out.append(f"  … {remaining} more")
    return out


def _fmt_resolved(resolved: Iterable[dict], max_items: int) -> list[str]:
    out: list[str] = []
    resolved = list(resolved)
    for r in resolved[:max_items]:
        cid = r.get("citation_id") or "?"
        doc = r.get("document_id") or "?"
        page = r.get("page")
        section = r.get("section")
        bits = [f"  [{cid}] → {doc}"]
        if section:
            bits.append(f"§{section}")
        if page is not None:
            bits.append(f"(p. {page})")
        out.append(" ".join(bits))
    remaining = max(0, len(resolved) - max_items)
    if remaining:
        out.append(f"  … {remaining} more")
    return out


def _fmt_entities(entities: dict) -> str:
    parts: list[str] = []
    for kind in sorted(entities.keys()):
        vals = entities.get(kind) or []
        if isinstance(vals, list) and vals:
            parts.append(f"{kind}={','.join(str(v) for v in vals[:3])}")
    return "; ".join(parts)


def _q(text: str) -> str:
    text = text.replace("\n", " ").strip()
    return f"\"{text}\""
