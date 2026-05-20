from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

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


def _split_model_id(raw: str, *, default_variant: str, default_llm: str) -> tuple[str, str]:
    """Parse `<variant>@<llm>`. Missing parts fall back to defaults."""
    if not raw:
        return default_variant, default_llm
    variant, sep, llm = raw.partition("@")
    if not sep:
        # Treat a bare id as variant only.
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
        allowed = getattr(runner, "compatible_llms", None)
        for llm_id in llm_ids:
            if allowed is not None and llm_id not in allowed:
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
async def chat_completions(req: ChatCompletionRequest, request: Request) -> dict[str, Any]:
    container = request.app.state.container
    settings = container.settings

    variant_id, llm_id = _split_model_id(
        req.model,
        default_variant=settings.default_variant,
        default_llm=settings.default_llm,
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

    allowed = getattr(runner, "compatible_llms", None)
    if allowed is not None and llm_id not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "incompatible_llm",
                    "message": f"variant {variant_id!r} cannot use llm {llm_id!r}",
                    "compatible_llms": sorted(allowed),
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
    response = await runner.run(agent_request)

    resolved_llm = response.llm_id or llm_id
    composite_id = f"{variant_id}@{resolved_llm}"

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
        "smr_agent": {
            "interaction_id": interaction_id,
            "agent_variant": runner.variant_id,
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
        },
    }
