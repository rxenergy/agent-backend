from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.domain.interaction import ChatTurn
from app.domain.memory import RetrievalTrace, SessionState, TrackedReference
from app.domain.tools import ToolResult
from app.ports.session_state_store import SessionStateStore
from app.ports.tool import ToolExecutionContext

# 미등장 참조의 salience 감쇠 계수(turn 당). 오래된 참조가 자연 강등돼 무한 누적/오염을
# 막는다(salience-based eviction — 설계 §7). salience 가 이 floor 미만이면 evict.
_SALIENCE_DECAY = 0.8
_SALIENCE_FLOOR = 0.2
# 참조 재등장 시 가산(상한 없음 — recency·반복이 누적). 신규=1.0.
_SALIENCE_BUMP = 0.5


class SessionLoadInput(BaseModel):
    session_id: str | None = None


class _RefInput(BaseModel):
    ref_id: str
    ref_type: str = "reference"
    label: str = ""


class SessionUpdateInput(BaseModel):
    """범용 세션 갱신 입력. 누적 로직(turn_count·salience·윈도우·variant merge)은
    도구 내부가 책임진다 — 모든 variant 가 동일 누적 규칙을 공유한다(설계 §3.2)."""

    session_id: str
    variant_id: str
    user_turn: str = ""
    assistant_turn: str = ""
    new_references: list[_RefInput] = Field(default_factory=list)
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    retrieved_source_ids: list[str] = Field(default_factory=list)
    # None=미갱신(호출자가 summarizer 를 돌리지 않았거나 압축 불요). 빈 문자열도 미갱신.
    running_summary: str | None = None
    topic_signature: str | None = None
    memory_ids_used: list[str] = Field(default_factory=list)
    # 이 variant namespace 에 shallow-merge 할 상태(spec_driven=route/authority 등).
    variant_state: dict[str, Any] = Field(default_factory=dict)
    keep_turns: int = 10
    retrieval_window: int = 5


class SessionLoadTool:
    name = "memory.session_load"
    version = "v1"

    def __init__(self, store: SessionStateStore) -> None:
        self._store = store

    async def invoke(
        self,
        tool_input: SessionLoadInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = SessionLoadInput.model_validate(tool_input)
        sid = tool_input.session_id
        if not sid:
            return self._absent(context)
        state = await self._store.get(sid)
        if state is None:
            return self._absent(context)
        expires_at = state.expires_at
        if expires_at and expires_at < datetime.now(tz=timezone.utc):
            return self._absent(context, reason="expired")
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={
                "present": True,
                "turn_count": state.turn_count,
                "last_variant_id": state.last_variant_id,
                "running_summary": state.running_summary,
                "topic_signature": state.topic_signature,
                "recent_turns": [
                    {"role": t.role, "content": t.content} for t in state.recent_turns
                ],
                # salience 내림차순 — 호출자(게이트/anaphora)가 상위만 쓰기 쉽게.
                "tracked_references": [
                    {
                        "ref_id": r.ref_id,
                        "ref_type": r.ref_type,
                        "label": r.label,
                        "salience": r.salience,
                        "last_turn": r.last_turn,
                    }
                    for r in sorted(
                        state.tracked_references, key=lambda r: r.salience, reverse=True
                    )
                ],
                "retrieval_history": [
                    {
                        "turn_index": h.turn_index,
                        "chunk_ids": h.chunk_ids,
                        "source_ids": h.source_ids,
                    }
                    for h in state.retrieval_history
                ],
                "last_memory_ids_used": state.last_memory_ids_used,
                "variant_state": state.variant_state,
            },
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )

    def _absent(self, context: ToolExecutionContext, reason: str | None = None) -> ToolResult:
        output: dict[str, Any] = {"present": False}
        if reason:
            output["reason"] = reason
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output,
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )


class SessionUpdateTool:
    name = "memory.session_update"
    version = "v1"

    def __init__(self, store: SessionStateStore, ttl_days: int) -> None:
        self._store = store
        self._ttl_days = ttl_days

    async def invoke(
        self,
        tool_input: SessionUpdateInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = SessionUpdateInput.model_validate(tool_input)

        now = datetime.now(tz=timezone.utc)
        prior = await self._store.get(tool_input.session_id)
        if prior is None:
            prior = SessionState(
                session_id=tool_input.session_id,
                user_id=context.user_id,
                project_id=context.project_id,
            )

        new_turn_index = prior.turn_count + 1

        # --- 대화 코어 ---
        turns = list(prior.recent_turns)
        if tool_input.user_turn:
            turns.append(ChatTurn(role="user", content=tool_input.user_turn))
        if tool_input.assistant_turn:
            turns.append(ChatTurn(role="assistant", content=tool_input.assistant_turn))
        turns = turns[-tool_input.keep_turns :]

        # --- tracked_references: salience decay + upsert ---
        refs = _accumulate_references(
            prior.tracked_references, tool_input.new_references, new_turn_index
        )

        # --- retrieval_history: sliding window ---
        history = list(prior.retrieval_history)
        if tool_input.retrieved_chunk_ids or tool_input.retrieved_source_ids:
            history.append(
                RetrievalTrace(
                    turn_index=new_turn_index,
                    chunk_ids=list(tool_input.retrieved_chunk_ids),
                    source_ids=list(tool_input.retrieved_source_ids),
                )
            )
        history = history[-tool_input.retrieval_window :]

        # --- variant_state: namespace shallow-merge ---
        variant_state = dict(prior.variant_state)
        if tool_input.variant_state:
            ns = dict(variant_state.get(tool_input.variant_id, {}))
            ns.update(tool_input.variant_state)
            variant_state[tool_input.variant_id] = ns

        # running_summary 는 None/빈문자열이면 미갱신(prior 보존).
        running_summary = (
            tool_input.running_summary
            if tool_input.running_summary
            else prior.running_summary
        )

        state = SessionState(
            session_id=tool_input.session_id,
            user_id=context.user_id or prior.user_id,
            project_id=context.project_id or prior.project_id,
            last_variant_id=tool_input.variant_id,
            turn_count=new_turn_index,
            recent_turns=turns,
            running_summary=running_summary,
            tracked_references=refs,
            retrieval_history=history,
            topic_signature=tool_input.topic_signature or prior.topic_signature,
            last_memory_ids_used=list(tool_input.memory_ids_used),
            variant_state=variant_state,
            updated_at=now,
            expires_at=now + timedelta(days=self._ttl_days),
        )
        await self._store.upsert(state)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={
                "session_id": tool_input.session_id,
                "turn_count": new_turn_index,
                "num_references": len(refs),
                "ttl_days": self._ttl_days,
            },
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )


def _accumulate_references(
    prior: list[TrackedReference],
    new_refs: list[_RefInput],
    turn_index: int,
) -> list[TrackedReference]:
    """salience decay(미등장) + upsert(재등장/신규). ref_id 키. floor 미만은 evict.

    결정론(silent 동작 없음) — 입력 순서·decay 규칙이 고정이라 재현 가능."""
    new_ids = {r.ref_id for r in new_refs}
    by_id: dict[str, TrackedReference] = {}
    # 기존 참조: 이번 턴에 등장하지 않으면 decay.
    for r in prior:
        if r.ref_id in new_ids:
            by_id[r.ref_id] = r  # 아래 bump 단계에서 갱신
        else:
            decayed = r.salience * _SALIENCE_DECAY
            if decayed >= _SALIENCE_FLOOR:
                by_id[r.ref_id] = TrackedReference(
                    ref_id=r.ref_id,
                    ref_type=r.ref_type,
                    label=r.label,
                    first_turn=r.first_turn,
                    last_turn=r.last_turn,
                    salience=decayed,
                )
            # floor 미만 → evict(drop).
    # 신규/재등장 참조: bump + last_turn 갱신.
    for nr in new_refs:
        existing = by_id.get(nr.ref_id)
        if existing is not None:
            by_id[nr.ref_id] = TrackedReference(
                ref_id=existing.ref_id,
                ref_type=existing.ref_type or nr.ref_type,
                label=existing.label or nr.label,
                first_turn=existing.first_turn,
                last_turn=turn_index,
                salience=existing.salience + _SALIENCE_BUMP,
            )
        else:
            by_id[nr.ref_id] = TrackedReference(
                ref_id=nr.ref_id,
                ref_type=nr.ref_type,
                label=nr.label,
                first_turn=turn_index,
                last_turn=turn_index,
                salience=1.0,
            )
    return list(by_id.values())
