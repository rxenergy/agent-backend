"""Translate AgentEvents into human-readable reasoning lines.

Surfaced through OpenAI-compatible `delta.reasoning_content` (streaming) or
`<think>…</think>` content prefix (non-streaming) so that OpenWebUI renders
agent workflow progress in its collapsible thinking block.

Style: first-person present-continuous for in-flight steps + past-tense
single-line summaries on completion, with indented bullet lists for the
detail of each step (search query, retrieved documents, resolved citations,
…). Modeled after OpenAI Deep Research / o1 reasoning traces and Perplexity
Pro Search progress lines. Domain nouns stay in English so the thinking
layer reads consistently when the upstream LLM also emits reasoning_content.

The final assistant answer (`response.answer_text`) is unaffected — the
renderer only produces the auxiliary thinking surface.
"""
from __future__ import annotations

from typing import Iterable, Literal

from app.application.agents.events import AgentEvent

ContentMode = Literal["metadata", "snippets", "full"]


def render(
    event: AgentEvent,
    *,
    content_mode: ContentMode = "metadata",
    max_items: int = 3,
) -> list[str]:
    """Return zero or more thinking lines for an event.

    Pure function — no side effects, no I/O. Empty list means "drop this
    event" (no thinking output). Each returned string is one logical line
    (callers add a trailing newline).
    """
    if event.kind == "step":
        return _render_step(event, content_mode=content_mode, max_items=max_items)
    if event.kind == "tool":
        return _render_tool(event)
    return []


def _render_step(
    event: AgentEvent, *, content_mode: ContentMode, max_items: int
) -> list[str]:
    name = event.name or ""
    status = event.status or ""
    p = event.payload or {}

    if name == "intent_classification":
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

    if name == "session_memory_load":
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

    if name == "memory_approved_search":
        if status == "started":
            return ["Searching approved memory from prior expert-reviewed answers."]
        if status == "ok":
            n = p.get("hit_count", 0)
            if n == 0:
                return ["No approved memory matched this scenario."]
            lines = [f"Matched {n} approved memory item(s) from prior expert-reviewed answers."]
            lines.extend(_fmt_memory_hits(p.get("hits_preview") or [], max_items))
            return lines

    if name == "retrieval":
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

    if name == "context_build":
        if status == "started":
            return ["Assembling the context pack — passages, citations, and memory."]
        if status == "ok":
            return ["Context pack assembled."]

    if name == "prompt_render":
        if status == "started":
            return ["Rendering the prompt template for this scenario/depth profile."]
        if status == "ok":
            pid = p.get("profile_id")
            ver = p.get("profile_version")
            if pid and ver:
                return [f"Prompt rendered (profile {pid}@{ver})."]
            return ["Prompt rendered."]

    if name == "generation":
        if status == "started":
            llm = p.get("llm_id")
            return [f"Drafting the answer with {llm}." if llm else "Drafting the answer now."]
        if status == "ok":
            return []  # token stream itself carries the visible progress

    if name == "citation_resolve":
        if status == "started":
            return ["Resolving citations to source documents."]
        if status == "ok":
            n = p.get("resolved_count", 0)
            total = p.get("total", n)
            lines = [f"Resolved {n} of {total} citation(s) to source documents:"]
            lines.extend(_fmt_resolved(p.get("resolved_preview") or [], max_items))
            return lines

    if name == "verification":
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
