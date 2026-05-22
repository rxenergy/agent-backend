from __future__ import annotations

import time
from typing import AsyncIterator

from app.application.agents.events import AgentEvent
from app.application.agents.registry import AgentDeps, register_variant
from app.application.events.recorder import EventRecorder
from app.domain.agents import VariantSpec
from app.domain.errors import VerificationStatus
from app.domain.interaction import AgentRequest, AgentResponse, Citation
from app.observability.otel import get_tracer

_TRACER = get_tracer("agent")

FAKE_ECHO_VARIANT_ID = "fake_echo_v0"


class FakeEchoAgentRunner:
    """P0 test variant — single span, no tools."""

    def __init__(self, recorder: EventRecorder, spec: VariantSpec) -> None:
        self._recorder = recorder
        self.spec = spec

    async def run(self, request: AgentRequest) -> AgentResponse:
        started = time.monotonic()
        with _TRACER.start_as_current_span("agent.run") as span:
            span.set_attribute("agent.variant", self.spec.variant_id)
            span.set_attribute("interaction_id", request.interaction_id)
            answer = f"[echo] {request.query_text}"
            citations = (
                Citation(
                    citation_id="cite-0",
                    chunk_id="chunk-fake-0",
                    document_id="doc-fake",
                    page=1,
                    score=1.0,
                ),
            )
            response = AgentResponse(
                interaction_id=request.interaction_id,
                answer_text=answer,
                citations=citations,
                refusal_reason=None,
                verification_status=VerificationStatus.SKIPPED.value,
                scenario_object=None,
                scenario_depth=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                token_usage={
                    "prompt_tokens": len(request.query_text),
                    "completion_tokens": len(answer),
                },
            )

        event = self._recorder.build(
            request=request,
            response=response,
            agent_variant=self.spec.variant_id,
            started_at=started,
        )
        await self._recorder.persist(event)
        return response

    async def run_stream(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentEvent]:
        """Minimal Protocol-compliant stream: run to completion and yield a
        single `final` event. Token-level streaming is intentionally absent
        for the P0 variant."""
        response = await self.run(request)
        yield AgentEvent(
            kind="final",
            payload={"response": response},
            ts=time.monotonic(),
        )


@register_variant(FAKE_ECHO_VARIANT_ID)
def _build_fake_echo(spec: VariantSpec, deps: AgentDeps) -> FakeEchoAgentRunner:
    return FakeEchoAgentRunner(recorder=deps.recorder, spec=spec)
