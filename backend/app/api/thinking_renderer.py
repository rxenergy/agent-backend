"""Translate AgentEvents into human-readable reasoning lines.

Surfaced through OpenAI-compatible `delta.reasoning_content` (streaming) or
`<think>…</think>` content prefix (non-streaming) so that OpenWebUI renders
agent workflow progress in its collapsible thinking block.

The sole shipped variant (`spec_driven_v1`) builds its thinking surface from
the LLM nodes' own output — native CoT or a structured `reasoning` field
emitted as `reasoning` events — not from deterministic step/tool narration. For
those variants `render()` short-circuits to [] so no narration line is produced;
the `reasoning` events go straight to `delta.reasoning_content` at the API layer
(renderer-bypassed). See spec_driven_thinking_output.design.v1.md.

`tool` events are the one exception: a failed tool call is surfaced (it explains
an imminent refusal / fallback) regardless of variant. The final assistant
answer (`response.answer_text`) is unaffected by this renderer.
"""
from __future__ import annotations

from typing import Literal

from app.application.agents.events import AgentEvent

ContentMode = Literal["metadata", "snippets", "full"]
Verbosity = Literal["summary", "detailed", "off"]

# Variants whose thinking surface is the LLM nodes' own output (native CoT or a
# structured `reasoning` field), not deterministic step/tool narration. For
# these, `render()` short-circuits to [] for `step` events; the `reasoning`
# events the runner emits go straight to `delta.reasoning_content` at the API
# layer (renderer-bypassed). See spec_driven_thinking_output.design.v1.md.
_LLM_THINKING_VARIANTS: frozenset[str] = frozenset({"spec_driven_v1"})


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

    Deterministic workflow `step` events are not narrated for the shipped
    variants (their thinking is the LLM nodes' own `reasoning` output, carried
    on a separate channel). Failed `tool` events are surfaced so a refusal /
    fallback is explained.
    """
    if verbosity == "off":
        return []
    if event.kind == "tool":
        return _render_tool(event)
    # `step` events: the shipped variant routes thinking through `reasoning`
    # events (renderer-bypassed at the API layer), so produce no narration.
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
