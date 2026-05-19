from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
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


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    settings = request.app.state.container.settings
    return {
        "object": "list",
        "data": [
            {
                "id": settings.exposed_model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "smr-agent",
            }
        ],
    }


def _last_user_query(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return ""


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request) -> dict[str, Any]:
    container = request.app.state.container
    settings = container.settings
    runner = container.runner

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
        model=req.model,
        model_options=model_options,
        session_id=request.headers.get("x-session-id"),
        user_id=request.headers.get("x-user-id"),
        project_id=request.headers.get("x-project-id"),
    )
    response = await runner.run(agent_request)

    return {
        "id": interaction_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": settings.exposed_model_id,
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
