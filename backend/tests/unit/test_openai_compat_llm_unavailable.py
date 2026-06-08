"""LLM 백엔드 미도달(모델 다운/네트워크 장애)은 명료화 답변(200 content)으로
둔갑하지 않고 OpenAI 스펙 에러로 반환된다:
  • 비스트리밍 → HTTP 503 + top-level {"error": {type:server_error, code:llm_unavailable}}
  • 스트리밍   → 상태 content 라인 + finish="error" 프레임(200 은 오프닝 프레임에서
                 이미 커밋되므로 in-band). "이해했습니다" 류 오해 라인은 나오지 않는다.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeEchoLLM
from app.api import openai_compat
from app.config.profiles import AppContainer
from app.config.settings import Settings
from app.domain.agents import VariantSpec
from app.domain.errors import RefusalReason
from app.domain.interaction import AgentResponse


def _unavailable_response(request) -> AgentResponse:
    # classification 단계에서 LLM 미도달 → runner 가 LLM_UNAVAILABLE 로 종결한 모양.
    # scenario 는 fallback 기본값(O4/D2)이지만 API 는 이를 노출하지 않고 에러로 변환.
    return AgentResponse(
        interaction_id=request.interaction_id,
        answer_text="모델을 가져올 수 없습니다.",  # 본문은 무시되고 에러로 대체돼야 함
        citations=(),
        refusal_reason=RefusalReason.LLM_UNAVAILABLE.value,
        verification_status="skipped",
        scenario_object="O4",
        scenario_depth="D2",
        latency_ms=1,
        token_usage={},
        llm_id="fake-echo",
        model_id="fake-echo",
    )


class _UnavailableRunner:
    spec = VariantSpec(
        variant_id="hierarchical_corrective_v3_1", compatible_llms=("fake-echo",)
    )

    async def run_stream(self, request):
        from app.application.agents.events import AgentEvent
        # 모델 미도달 경로는 토큰을 스트리밍하지 않는다(분류 단계에서 단락).
        yield AgentEvent(kind="final",
                         payload={"response": _unavailable_response(request)})

    async def run(self, request):
        return _unavailable_response(request)


def _app() -> FastAPI:
    tmp = tempfile.mkdtemp()
    sink = FilesystemEventSink(root=str(Path(tmp) / "events"), prefix="t")
    variant = "hierarchical_corrective_v3_1"
    settings = Settings(
        agent_variants_enabled=[variant],
        default_variant=variant,
        default_llm="fake-echo",
        utility_llm="fake-echo",
        thinking_expose=False,
    )
    app = FastAPI()
    app.include_router(openai_compat.router)
    app.state.container = AppContainer(
        settings=settings,
        runners={variant: _UnavailableRunner()},
        llm_pool={"fake-echo": FakeEchoLLM(model_id="fake-echo")},
        event_sink=sink,
    )
    return app


def _sse_chunks(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:].strip()))
    return out


def test_non_streaming_returns_503_openai_error():
    client = TestClient(_app())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "hierarchical_corrective_v3_1@fake-echo",
              "messages": [{"role": "user", "content": "q"}]},
    )
    assert resp.status_code == 503
    body = resp.json()
    # OpenAI 스펙: top-level error 봉투(detail 중첩 아님).
    assert set(body.keys()) == {"error"}
    err = body["error"]
    assert err["type"] == "server_error"
    assert err["code"] == "llm_unavailable"
    assert err["param"] is None
    # 사용자 메시지는 상태를 설명하되 내부 원인(hostname/Errno)은 싣지 않는다.
    assert "모델" in err["message"]
    assert "Errno" not in err["message"] and "vllm" not in err["message"]


def test_streaming_emits_status_line_and_error_finish():
    client = TestClient(_app())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "hierarchical_corrective_v3_1@fake-echo", "stream": True,
              "messages": [{"role": "user", "content": "q"}]},
    )
    assert resp.status_code == 200  # 오프닝 프레임에서 이미 커밋(in-band 처리)
    chunks = _sse_chunks(resp.text)
    content = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    # 사람이 보는 상태 라인이 렌더된다(명료화 답변 아님).
    assert "연결할 수 없" in content
    assert "이해했습니다" not in content
    # 구조화 종결: finish="error" + OpenAI 에러 봉투(smr_agent.error).
    err_frame = next(
        c for c in chunks if c["choices"][0].get("finish_reason") == "error"
    )
    assert err_frame["smr_agent"]["error"]["code"] == "llm_unavailable"
    assert err_frame["smr_agent"]["error"]["type"] == "server_error"
