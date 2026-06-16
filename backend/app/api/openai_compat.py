from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.domain.errors import RefusalReason

from app.api.answer_renderer import (
    CiteStreamRewriter,
    answer_trailer,
    compose_answer_body,
)
from app.api.thinking_renderer import render as render_thinking
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


# OpenWebUI 보조 task(follow-up / title / tags / autocomplete 생성)는 일반 채팅과
# 같은 /v1/chat/completions 로 들어오며, 지시문 + 대화 전문을 `<chat_history>…
# </chat_history>` 블록에 싸서 단일 user 메시지로 보낸다. 이 메타 프롬프트를 그대로
# retriever 질의로 쓰면 지시문 토큰("suggest follow-up questions", "JSON" 등)과
# 직전 답변 전문이 BM25/임베딩을 오염시킨다(분류·검색이 의미 없는 질의를 돈다).
# 검색·분류에 의미 있는 *마지막 실제 사용자 발화*로 환원한다. 서명(`<chat_history>`)이
# 없으면 일반 질의이므로 원문 그대로 둔다(no-op).
_CHAT_HISTORY_RE = re.compile(
    r"<chat_history>\s*(.*?)\s*</chat_history>", re.DOTALL | re.IGNORECASE
)
_TURN_RE = re.compile(r"^\s*(USER|ASSISTANT|SYSTEM)\s*:\s*(.*)$", re.IGNORECASE)


def _strip_task_scaffolding(text: str) -> str:
    """OpenWebUI task 메타 프롬프트를 검색·분류용 질의로 환원한다.

    `<chat_history>` 블록이 있으면 그 안의 마지막 USER 턴을 반환한다(가장 깨끗한
    검색 앵커). USER 턴이 없으면 지시문 scaffolding 을 제거한 대화 본문으로,
    그래도 비면 원문으로 폴백. 서명이 없으면 원문 그대로(일반 질의)."""
    m = _CHAT_HISTORY_RE.search(text)
    if not m:
        return text
    body = m.group(1)
    turns: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        tm = _TURN_RE.match(line)
        if tm:
            turns.append((tm.group(1).upper(), [tm.group(2)]))
        elif turns:
            turns[-1][1].append(line)
    for role, buf in reversed(turns):
        if role == "USER":
            joined = "\n".join(buf).strip()
            if joined:
                return joined
    return body.strip() or text


# LLM 백엔드 미도달(모델 다운/네트워크 장애)일 때 사용자에게 보여줄 상태 메시지.
# 내부 원인(DNS·hostname·Errno 등)은 절대 싣지 않는다 — 그건 로그/Phoenix span
# (classifier.upstream_error)에만 남기고, 사용자에겐 "질문 문제 아님 + 가용성 장애"만.
_LLM_UNAVAILABLE_MESSAGE = (
    "언어 모델 백엔드에 연결할 수 없어 요청을 처리하지 못했습니다. "
    "질문 내용의 문제가 아니라 모델 서비스 가용성 장애입니다. "
    "잠시 후 다시 시도하거나 관리자에게 문의해 주세요."
)


def _openai_error_body(
    message: str, *, type_: str, code: str, param: str | None = None
) -> dict[str, Any]:
    """OpenAI 에러 봉투(top-level `error`). 스펙: {message,type,param,code}."""
    return {"error": {"message": message, "type": type_, "param": param, "code": code}}


def _is_llm_unavailable(response) -> bool:
    return getattr(response, "refusal_reason", None) == RefusalReason.LLM_UNAVAILABLE.value


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
    # OpenWebUI task 메타 프롬프트는 검색을 오염시키므로 마지막 실제 사용자 발화로
    # 환원한다(일반 질의는 no-op). 분류·검색·생성이 모두 이 정제된 질의로 동작한다.
    query_text = _strip_task_scaffolding(_last_user_query(req.messages))
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

    thinking_expose = bool(settings.thinking_expose)
    content_mode = settings.trace_content_mode
    max_items = int(settings.thinking_max_items)
    verbosity = settings.thinking_verbosity

    if req.stream:
        return StreamingResponse(
            _sse_stream_from_runner(
                runner=runner,
                agent_request=agent_request,
                interaction_id=interaction_id,
                variant_id=variant_id,
                fallback_llm=llm_id,
                thinking_expose=thinking_expose,
                content_mode=content_mode,
                max_items=max_items,
                verbosity=verbosity,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if thinking_expose:
        response, thinking_lines = await _run_collecting_thinking(
            runner, agent_request, content_mode=content_mode,
            max_items=max_items, verbosity=verbosity,
        )
    else:
        response = await runner.run(agent_request)
        thinking_lines = []

    # 모델 백엔드 미도달 → OpenAI 스펙 에러(503)로 반환한다. 명료화 답변(200 content)
    # 으로 둔갑시키지 않는다 — 사용자가 "질문 문제"로 오인하지 않도록 상태를 명시.
    if _is_llm_unavailable(response):
        return JSONResponse(
            status_code=503,
            content=_openai_error_body(
                _LLM_UNAVAILABLE_MESSAGE, type_="server_error", code="llm_unavailable",
            ),
        )

    resolved_llm = response.llm_id or llm_id
    composite_id = f"{variant_id}@{resolved_llm}"
    smr_meta = _smr_agent_metadata(
        interaction_id=interaction_id,
        runner_variant=runner.spec.variant_id,
        resolved_llm=resolved_llm,
        response=response,
    )

    # boundary 가 인용 재번호 + References + 고지 callout 을 content 로 합성(decision A).
    content_text = compose_answer_body(response)
    if thinking_lines:
        think_block = "<think>\n" + "\n".join(thinking_lines) + "\n</think>\n\n"
        content_text = think_block + content_text

    return {
        "id": interaction_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": composite_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content_text},
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
        # 규제 근거 검증 축 — verification_status 와 직교. 구조화 클라이언트가
        # verification_status 만 읽고 v1 미검증 PASS 를 검증된 답으로 오인하지
        # 않도록 custom field 로도 노출(v3.1 안전 계약). 기본 "n_a"(v2 변형 외).
        "regulatory_grounding": getattr(response, "regulatory_grounding", "n_a"),
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
                # 원문 다운로드 URL(인덱스 doc_metadata 1차 소스) — dumb client 도
                # 출처를 직접 링크할 수 있게 노출(원칙 8). content 의 References 가
                # 이미 마크다운 링크를 싣지만, 구조화 소비자(eval/감사)도 URL 을 본다.
                "source_url": c.source_url,
                # 본문에서 분리된 표(원본 list — {tag,caption,markdown,html}). content
                # 의 References 가 이미 표를 마크다운/HTML 로 렌더하지만(OpenWebUI 가시),
                # 구조화 소비자(eval/감사)는 이 원본을 파싱한다(원칙 8 — silent 금지,
                # spec_driven_table_citation_references D7).
                "tables": c.tables,
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


async def _run_collecting_thinking(
    runner,
    agent_request: AgentRequest,
    *,
    content_mode: str,
    max_items: int,
    verbosity: str = "summary",
):
    """Drive `run_stream()` non-streaming side: return the final AgentResponse
    plus the list of human-readable thinking lines accumulated from step/tool
    events. Mirrors the streaming path so the two surfaces stay in sync.

    Step/tool events are narrated per variant by the thinking renderer; the
    generation LLM's native chain-of-thought (`reasoning` events) is buffered
    into one contiguous block and interleaved in occurrence order so the
    `<think>` block carries the model's reasoning alongside the workflow steps
    (the streaming path passes reasoning through directly). `token` events are
    the answer body and live in `response.answer_text`, not `<think>`."""
    response = None
    thinking: list[str] = []
    variant_id = runner.spec.variant_id
    reasoning_buf: list[str] = []

    def _flush_reasoning() -> None:
        if not reasoning_buf:
            return
        text = "".join(reasoning_buf).strip()
        reasoning_buf.clear()
        if text:
            thinking.append(text)

    async for event in runner.run_stream(agent_request):
        if event.kind == "final":
            response = event.payload["response"]
            continue
        if event.kind == "error":
            raise RuntimeError(event.payload.get("message") or "agent error")
        if event.kind == "reasoning":
            reasoning_buf.append(event.payload.get("content", ""))
            continue
        if event.kind == "token":
            continue
        _flush_reasoning()
        lines = render_thinking(
            event, variant_id=variant_id,
            content_mode=content_mode, max_items=max_items, verbosity=verbosity,
        )
        thinking.extend(lines)
    _flush_reasoning()
    if response is None:
        raise RuntimeError("runner produced no final response")
    return response, thinking


async def _sse_stream_from_runner(
    *,
    runner,
    agent_request: AgentRequest,
    interaction_id: str,
    variant_id: str,
    fallback_llm: str,
    thinking_expose: bool = True,
    content_mode: str = "metadata",
    max_items: int = 3,
    verbosity: str = "summary",
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
    # 본문 토큰 스트림의 [cite-N] → [n] 치환기. renumber 맵을 증분 구축 → 종료 후
    # trailer(References)가 동일 번호 사용(answer_renderer).
    cite_rewriter = CiteStreamRewriter()
    try:
        async for event in runner.run_stream(agent_request):
            if event.kind == "token":
                tokens_streamed = True
                rewritten = cite_rewriter.feed(event.payload.get("content", ""))
                if rewritten:  # 부분 [cite 홀드백 시 빈 문자열 → 프레임 생략.
                    yield _frame(
                        interaction_id=interaction_id,
                        composite_id=composite_id,
                        created=created,
                        delta={"content": rewritten},
                    )
            elif event.kind == "reasoning":
                yield _frame(
                    interaction_id=interaction_id,
                    composite_id=composite_id,
                    created=created,
                    delta={"reasoning_content": event.payload.get("content", "")},
                )
            elif event.kind in ("step", "tool"):
                if thinking_expose:
                    for line in render_thinking(
                        event, variant_id=runner.spec.variant_id,
                        content_mode=content_mode, max_items=max_items,
                        verbosity=verbosity,
                    ):
                        yield _frame(
                            interaction_id=interaction_id,
                            composite_id=composite_id,
                            created=created,
                            delta={"reasoning_content": line + "\n"},
                        )
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
                # 모델 백엔드 미도달 — 200 은 오프닝 프레임에서 이미 커밋되어 503 불가.
                # in-band 로 알린다: 사람이 보는 클라이언트(OpenWebUI)가 확실히 렌더하도록
                # content 상태 라인을 보내고, finish="error" + OpenAI 에러 봉투(smr.error)
                # 로 구조화 종결한다. 명료화 답변으로 둔갑시키지 않는다.
                if _is_llm_unavailable(response):
                    yield _frame(
                        interaction_id=interaction_id, composite_id=composite_id,
                        created=created, delta={"content": _LLM_UNAVAILABLE_MESSAGE},
                    )
                    yield _frame(
                        interaction_id=interaction_id, composite_id=composite_id,
                        created=created, delta={}, finish="error",
                        smr=_openai_error_body(
                            _LLM_UNAVAILABLE_MESSAGE, type_="server_error",
                            code="llm_unavailable",
                        ),
                    )
                    final_yielded = True
                    continue
                smr_meta = _smr_agent_metadata(
                    interaction_id=interaction_id,
                    runner_variant=runner.spec.variant_id,
                    resolved_llm=resolved_llm,
                    response=response,
                )
                if not tokens_streamed:
                    # 토큰이 없던 경로(거부/fake_echo): 전체 본문을 boundary 에서
                    # compose(마커 재번호 + References + 고지 callout) 해 단일 content.
                    composed = compose_answer_body(response)
                    if composed:
                        yield _frame(
                            interaction_id=interaction_id,
                            composite_id=composite_id,
                            created=created,
                            delta={"content": composed},
                        )
                else:
                    # 토큰이 스트리밍된 경로: 잔여 버퍼 flush 후 trailer(고지 callout +
                    # References)를 append. 순서 불변식 — 이 content 프레임은 반드시
                    # 아래 finish 프레임보다 *먼저* 나가야 OpenWebUI 가 드롭하지 않는다.
                    tail = cite_rewriter.flush()
                    if tail:
                        yield _frame(
                            interaction_id=interaction_id,
                            composite_id=composite_id,
                            created=created,
                            delta={"content": tail},
                        )
                    trailer = answer_trailer(response, cite_rewriter.renumber)
                    if trailer:
                        yield _frame(
                            interaction_id=interaction_id,
                            composite_id=composite_id,
                            created=created,
                            delta={"content": "\n\n" + trailer},
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
