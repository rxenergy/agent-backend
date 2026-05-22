from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.application.agents.events import AgentEvent
from app.domain.interaction import AgentRequest, ChatTurn

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool | None = False
    extra: dict[str, Any] = Field(default_factory=dict)


def _split_model_id(
    raw: str,
    *,
    default_variant: str,
    default_llm: str,
    known_llms: frozenset[str] = frozenset(),
    known_variants: frozenset[str] = frozenset(),
) -> tuple[str, str]:
    """Parse `<variant>@<llm>`. Missing parts fall back to defaults.

    A bare id (no `@`) is normally treated as a variant. As a convenience for
    OpenAI-compatible clients that only carry a single model field (e.g.
    OpenWebUI with bare LLM ids registered for side-by-side comparison), a bare
    id that matches a known LLM but not a known variant is interpreted as
    `(default_variant, raw)`.
    """
    if not raw:
        return default_variant, default_llm
    variant, sep, llm = raw.partition("@")
    if not sep:
        if raw not in known_variants and raw in known_llms:
            return default_variant, raw
        return (variant or default_variant), default_llm
    return (variant or default_variant), (llm or default_llm)


def _list_model_combinations(container) -> list[dict[str, Any]]:
    settings = container.settings
    runners = container.runners
    llm_ids = list(container.llm_pool.keys())
    now = int(time.time())

    pairs: list[tuple[str, str]] = []
    # Default combo first so OpenWebUI auto-selects it.
    default_pair = (settings.default_variant, settings.default_llm)
    if (
        default_pair[0] in runners
        and default_pair[1] in container.llm_pool
    ):
        pairs.append(default_pair)

    for variant_id, runner in runners.items():
        spec = runner.spec
        for llm_id in llm_ids:
            if not spec.accepts_llm(llm_id):
                continue
            pair = (variant_id, llm_id)
            if pair == default_pair:
                continue
            pairs.append(pair)

    return [
        {
            "id": f"{v}@{l}",
            "object": "model",
            "created": now,
            "owned_by": "smr-agent",
        }
        for v, l in pairs
    ]


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    container = request.app.state.container
    return {"object": "list", "data": _list_model_combinations(container)}


def _last_user_query(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return ""


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    container = request.app.state.container
    settings = container.settings

    variant_id, llm_id = _split_model_id(
        req.model,
        default_variant=settings.default_variant,
        default_llm=settings.default_llm,
        known_llms=frozenset(container.llm_pool.keys()),
        known_variants=frozenset(container.runners.keys()),
    )

    runner = container.runners.get(variant_id)
    if runner is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "unknown_variant",
                    "message": f"agent variant {variant_id!r} is not enabled",
                    "available": sorted(container.runners.keys()),
                }
            },
        )

    if llm_id not in container.llm_pool:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "unknown_llm",
                    "message": f"llm {llm_id!r} is not in the pool",
                    "available": sorted(container.llm_pool.keys()),
                }
            },
        )

    if not runner.spec.accepts_llm(llm_id):
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "incompatible_llm",
                    "message": f"variant {variant_id!r} cannot use llm {llm_id!r}",
                    "compatible_llms": sorted(runner.spec.compatible_llms),
                }
            },
        )

    interaction_id = str(uuid.uuid4())
    query_text = _last_user_query(req.messages)
    history = tuple(ChatTurn(role=m.role, content=m.content) for m in req.messages[:-1])

    model_options: dict[str, Any] = {}
    if req.temperature is not None:
        model_options["temperature"] = req.temperature
    if req.max_tokens is not None:
        model_options["max_tokens"] = req.max_tokens

    agent_request = AgentRequest(
        interaction_id=interaction_id,
        query_text=query_text,
        chat_history=history,
        model=llm_id,
        model_options=model_options,
        session_id=request.headers.get("x-session-id"),
        user_id=request.headers.get("x-user-id"),
        project_id=request.headers.get("x-project-id"),
    )

    if req.stream:
        return StreamingResponse(
            _sse_stream_from_runner(
                runner=runner,
                agent_request=agent_request,
                interaction_id=interaction_id,
                variant_id=variant_id,
                fallback_llm=llm_id,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    response = await runner.run(agent_request)
    resolved_llm = response.llm_id or llm_id
    composite_id = f"{variant_id}@{resolved_llm}"
    smr_meta = _smr_agent_metadata(
        interaction_id=interaction_id,
        runner_variant=runner.spec.variant_id,
        resolved_llm=resolved_llm,
        response=response,
    )

    return {
        "id": interaction_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": composite_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response.answer_text},
                "finish_reason": response.refusal_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": response.token_usage.get("prompt_tokens", 0),
            "completion_tokens": response.token_usage.get("completion_tokens", 0),
            "total_tokens": sum(response.token_usage.values()),
        },
        "smr_agent": smr_meta,
    }


def _smr_agent_metadata(
    *,
    interaction_id: str,
    runner_variant: str,
    resolved_llm: str,
    response,
) -> dict[str, Any]:
    return {
        "interaction_id": interaction_id,
        "agent_variant": runner_variant,
        "llm_id": resolved_llm,
        "model_id": response.model_id,
        "scenario_object": response.scenario_object,
        "scenario_depth": response.scenario_depth,
        "classification_confidence": response.classification_confidence,
        "classifier_backend": response.classifier_backend,
        "entities": response.entities,
        "verification_status": response.verification_status,
        "refusal_reason": response.refusal_reason,
        "citations": [
            {
                "citation_id": c.citation_id,
                "chunk_id": c.chunk_id,
                "document_id": c.document_id,
                "page": c.page,
                "section": c.section,
                "score": c.score,
                "doc_type": c.doc_type,
                "revision": c.revision,
                "response_date": c.response_date,
                "regulation_clause": c.regulation_clause,
                "formatted": c.formatted,
            }
            for c in response.citations
        ],
    }


def _frame(
    *,
    interaction_id: str,
    composite_id: str,
    created: int,
    delta: dict[str, Any],
    finish: str | None = None,
    smr: dict[str, Any] | None = None,
    usage: dict[str, int] | None = None,
) -> bytes:
    choice: dict[str, Any] = {"index": 0, "delta": delta, "finish_reason": finish}
    payload: dict[str, Any] = {
        "id": interaction_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": composite_id,
        "choices": [choice],
    }
    if usage is not None:
        payload["usage"] = usage
    if smr is not None:
        payload["smr_agent"] = smr
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _sse_stream_from_runner(
    *,
    runner,
    agent_request: AgentRequest,
    interaction_id: str,
    variant_id: str,
    fallback_llm: str,
) -> AsyncIterator[bytes]:
    """Translate `runner.run_stream()` events into OpenAI chat.completion.chunk
    SSE frames.

    Mapping:
      • token       → `delta.content`
      • reasoning   → `delta.reasoning_content` (DeepSeek / OpenWebUI convention)
      • step / tool → empty delta + `smr_agent.event` sidechannel
      • final       → terminal frame with `finish_reason`, `usage`, and full
                       `smr_agent` metadata (citations, verification, etc.)
      • error       → terminal frame with `finish_reason="error"`
    """
    composite_id = f"{variant_id}@{fallback_llm}"
    created = int(time.time())

    # Opening frame: role only (OpenAI convention).
    yield _frame(
        interaction_id=interaction_id,
        composite_id=composite_id,
        created=created,
        delta={"role": "assistant"},
    )

    final_yielded = False
    tokens_streamed = False
    try:
        async for event in runner.run_stream(agent_request):
            if event.kind == "token":
                tokens_streamed = True
                yield _frame(
                    interaction_id=interaction_id,
                    composite_id=composite_id,
                    created=created,
                    delta={"content": event.payload.get("content", "")},
                )
            elif event.kind == "reasoning":
                yield _frame(
                    interaction_id=interaction_id,
                    composite_id=composite_id,
                    created=created,
                    delta={"reasoning_content": event.payload.get("content", "")},
                )
            elif event.kind in ("step", "tool"):
                yield _frame(
                    interaction_id=interaction_id,
                    composite_id=composite_id,
                    created=created,
                    delta={},
                    smr=_event_to_smr(event),
                )
            elif event.kind == "final":
                response = event.payload["response"]
                resolved_llm = response.llm_id or fallback_llm
                composite_id = f"{runner.spec.variant_id}@{resolved_llm}"
                smr_meta = _smr_agent_metadata(
                    interaction_id=interaction_id,
                    runner_variant=runner.spec.variant_id,
                    resolved_llm=resolved_llm,
                    response=response,
                )
                # If no tokens streamed (early refusal, variants without
                # token-level streaming like fake_echo_v0, or post-verify
                # refusal-message overwrite), emit the full answer_text as
                # a single content chunk so OpenWebUI still renders a body.
                if response.answer_text and not tokens_streamed:
                    yield _frame(
                        interaction_id=interaction_id,
                        composite_id=composite_id,
                        created=created,
                        delta={"content": response.answer_text},
                    )
                yield _frame(
                    interaction_id=interaction_id,
                    composite_id=composite_id,
                    created=created,
                    delta={},
                    finish=response.refusal_reason or "stop",
                    smr={**smr_meta, "answer_text": response.answer_text},
                    usage={
                        "prompt_tokens": response.token_usage.get("prompt_tokens", 0),
                        "completion_tokens": response.token_usage.get("completion_tokens", 0),
                        "total_tokens": sum(response.token_usage.values()),
                    },
                )
                final_yielded = True
            elif event.kind == "error":
                yield _frame(
                    interaction_id=interaction_id,
                    composite_id=composite_id,
                    created=created,
                    delta={},
                    finish="error",
                    smr={"error": event.payload},
                )
                final_yielded = True
    except Exception as exc:  # noqa: BLE001 — terminal frame, then re-raise is not useful here
        if not final_yielded:
            yield _frame(
                interaction_id=interaction_id,
                composite_id=composite_id,
                created=created,
                delta={},
                finish="error",
                smr={"error": {"message": str(exc), "type": type(exc).__name__}},
            )

    yield b"data: [DONE]\n\n"


def _event_to_smr(event: AgentEvent) -> dict[str, Any]:
    return {
        "event": {
            "kind": event.kind,
            "name": event.name,
            "status": event.status,
            **event.payload,
        }
    }
