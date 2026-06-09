from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal["step", "tool", "token", "reasoning", "final", "error"]


@dataclass(frozen=True)
class AgentEvent:
    """Single unit of runner progress visible to the SSE layer.

    `step`/`tool` ride as `smr_agent.event` sidechannel frames in the
    OpenAI-compatible chunk stream — OpenWebUI ignores unknown fields, our
    own client renders them as a progress trace. `token`/`reasoning` map
    onto OpenAI `delta.content` / `delta.reasoning_content`. `final` carries
    the terminal AgentResponse + smr_agent metadata. `error` signals a
    mid-stream runner failure (HTTP status is already 200 at that point).
    """

    kind: EventKind
    name: str | None = None
    status: str | None = None  # "started" | "ok" | "error"
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


class EventEmitter:
    """asyncio.Queue-backed emit channel.

    Bound to the current asyncio task via ContextVar so runner code can call
    the module-level helpers without threading an emitter through every
    method signature. A no-op emitter is installed when no consumer is
    listening, so `run()` (non-streaming) pays nothing.
    """

    def __init__(self, *, active: bool) -> None:
        self._queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._active = active

    @property
    def active(self) -> bool:
        return self._active

    async def emit(self, event: AgentEvent) -> None:
        if self._active:
            await self._queue.put(event)

    async def close(self) -> None:
        if self._active:
            await self._queue.put(None)

    async def drain(self):
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev


_NOOP = EventEmitter(active=False)
_current: ContextVar[EventEmitter] = ContextVar("agent_emitter", default=_NOOP)


def current_emitter() -> EventEmitter:
    return _current.get()


def bind_emitter(emitter: EventEmitter):
    """Install `emitter` on the current asyncio task. Returns a token to
    pass back to `unbind_emitter` after the run completes."""
    return _current.set(emitter)


def unbind_emitter(token) -> None:
    _current.reset(token)


async def emit_step(name: str, status: str, **payload: Any) -> None:
    em = _current.get()
    if em.active:
        await em.emit(AgentEvent(kind="step", name=name, status=status,
                                 payload=payload, ts=time.monotonic()))


async def emit_tool(name: str, status: str, **payload: Any) -> None:
    em = _current.get()
    if em.active:
        await em.emit(AgentEvent(kind="tool", name=name, status=status,
                                 payload=payload, ts=time.monotonic()))


async def emit_token(content: str) -> None:
    em = _current.get()
    if em.active and content:
        await em.emit(AgentEvent(kind="token", payload={"content": content},
                                 ts=time.monotonic()))


async def emit_reasoning(content: str) -> None:
    em = _current.get()
    if em.active and content:
        await em.emit(AgentEvent(kind="reasoning", payload={"content": content},
                                 ts=time.monotonic()))


class LazyReasoning:
    """Emit `reasoning` deltas under a one-time, lazy phase header.

    A spec_driven_v1-style node (define_spec / query_formulation / generation)
    may or may not produce reasoning — native CoT only flows from reasoning
    models, and the structured-rationale backstop may be empty. The header
    (`**<label>**`) is emitted *only* on the first non-empty reasoning text, so
    a node that produces no reasoning leaves no empty header in the OpenWebUI
    Thought block (design D5). `emitted` lets the caller decide whether to fall
    back to the structured `reasoning` field after native CoT came up empty
    (design D2/D3 — one source per node, no duplication).
    """

    def __init__(self, label: str | None) -> None:
        self._label = label
        self._opened = False
        self.emitted = False

    async def feed(self, text: str) -> None:
        if not text:
            return
        if not self._opened and self._label:
            await emit_reasoning(f"\n**{self._label}**\n")
            self._opened = True
        await emit_reasoning(text)
        self.emitted = True


def emit_step_nowait(name: str, status: str, **payload: Any) -> None:
    """Sync variant for use inside non-async helpers. Queue is unbounded so
    `put_nowait` never blocks."""
    em = _current.get()
    if em.active:
        em._queue.put_nowait(  # noqa: SLF001 — internal sibling helper
            AgentEvent(kind="step", name=name, status=status,
                       payload=payload, ts=time.monotonic())
        )


def emit_tool_nowait(name: str, status: str, **payload: Any) -> None:
    em = _current.get()
    if em.active:
        em._queue.put_nowait(  # noqa: SLF001
            AgentEvent(kind="tool", name=name, status=status,
                       payload=payload, ts=time.monotonic())
        )
