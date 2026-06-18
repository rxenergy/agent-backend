"""SlotVerifierPort 구현체 — :class:`~app.ports.llm.LLMPort`(Node2 vLLM)로 슬롯 1개의
1차 검색 결과를 검증한다(spec_driven_v2 Node2 = sub, follow_up 과 같은 노드).

검증은 슬롯 1개의 청크 전체를 **하나의 프롬프트로 합쳐 단일 LLM 호출**로 판정한다
(guided decoding, `GrammarSpec(kind="json_schema")`). 모델은 청크 id 만 가리켜 necessary/
multihop 두 식별자 리스트를 직접 산출한다(verify_slot_v2). 어댑터(HttpLLM)가 vLLM
`guided_json` 으로 변환하므로 이 모듈은 외부 LLM SDK 를 직접 import 하지 않는다(원칙 #4).

동시 슬롯 호출 수는 인스턴스 소유 semaphore 로 캡한다 — 러너가 슬롯을 동시에 fan-out 해도
Node2 vLLM 에 나가는 호출 총량을 전역으로 묶는다(`max_num_seqs` 초과 발사 → 큐 적체·타임아웃
캐스케이드 방지). verify·follow_up 이 같은 Node2 를 공유하므로 두 도구의 동시 호출이 같은
vLLM 예산을 두고 경쟁한다. RetrievalVerifySlotTool 의 도구 레벨 캡과 중복이나, 어댑터 경계의 안전판.

출력은 청크 *식별자*다(address-not-content 불변): 답변에 꼭 필요한 청크 + 멀티홉(재검색)
필요 청크. 모델이 입력에 없는 id 를 내면 입력 부분집합으로 필터한다(hallucinated id 제거).
LLM 미가용/파싱 실패는 슬롯 단위로 안전 degrade — 그 슬롯 청크를 *전량* necessary 로 보존하고
멀티홉을 비워(재검색 안 함 → 단일노드 동작과 동형) method="fallback" 을 낸다. 이때 rationale 에
실패 사유를 실어 러너가 UI thinking 에 "검증 호출 실패 → 전량 보존" 을 노출하게 한다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import ChatMessage, GrammarSpec, LLMPort

_TRACER = get_tracer("agent")

# 청크 본문 렌더 캡(char). verify 는 "필요/멀티홉 판정"만 하므로 전문이 필요 없다 —
# snippet 길이로 충분하고, Node2 KV-cache·디코딩 비용을 줄인다(공유 vLLM 안전판). 슬롯
# 일괄이라 청크 수만큼 곱해지나(≤20×1200자) Node2 context 안에 든다.
_CHUNK_RENDER_CHARS = 1200

# 동시 슬롯 호출 수 상한 — 러너 fan-out 전체에 걸친 *전역* 캡(인스턴스 싱글톤이라 모든
# 슬롯이 self._sem 을 공유). 슬롯당 호출 1회라 동시 호출 = 동시 슬롯 수. Node2 vLLM 의
# continuous batching 이 동시 요청을 한 GPU step 에 묶으므로 VLLM_MAX_NUM_SEQS 여유 안에서
# 넉넉히 둔다. 너무 높이면 큐 적체 → per-call 타임아웃·재시도 캐스케이드. 환경별 최적값은
# SPEC_DRIVEN_MAX_QUERIES 로 조정한다(profiles.py 가 그 값을 max_concurrency 로 주입).
_MAX_CONCURRENCY = 48

# rationale 길이 가드 — 모델의 단일 rationale 문자열을 그대로 싣는다. UI thinking 노출용이라
# 과도한 bloat 만 막는다(총 char cap).
_RATIONALE_CHARS_CAP = 4000

# 검증 호출 실패 시 UI thinking·이벤트에 남길 사유 문구의 prefix(러너가 fallback marker 와
# 함께 노출). 예외 메시지를 일부 덧붙여 어떤 실패였는지 추적 가능하게 한다.
_FALLBACK_RATIONALE_PREFIX = "⚠ Node2 검증 호출 실패 — 이 슬롯 청크 전량 보존"


class SlotVerifierLlm:
    """SlotVerifierPort 구현체 — Node2 LLMPort 주입형(async, 슬롯당 단일 호출).

    `source` 는 registry 호스팅 verify(slot) 프롬프트(prompt_body + json_schema +
    model_options). boot 시 sha 검증된 source 를 그대로 주입받는다(프롬프트 인라인 금지 —
    원칙 5). `max_concurrency` 는 동시 슬롯 호출 전역 캡."""

    def __init__(
        self, *, llm: LLMPort, source: Any, max_concurrency: int | None = None
    ) -> None:
        self._llm = llm
        self._source = source
        self._sem = asyncio.Semaphore(max_concurrency or _MAX_CONCURRENCY)

    async def verify_slot(
        self,
        *,
        query_text: str,
        answer_spec: str,
        slot_name: str,
        slot_query: str,
        chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # chunk_id 가 있는 청크만 판정 대상. 빈 입력은 호출 낭비 — 러너가 0건 슬롯을 스킵하지만
        # 방어적으로 한 번 더(fallback 빈 dict).
        verifiable = [c for c in chunks if c.get("chunk_id")]
        all_ids = [str(c.get("chunk_id")) for c in verifiable]
        if not all_ids:
            return {"necessary_chunk_ids": [], "multihop_chunk_ids": [],
                    "rationale": "", "method": "fallback"}

        model_options = dict(self._source.model_options or {})
        user_content = self._render_user(
            query_text, answer_spec, slot_name, slot_query, verifiable
        )
        try:
            async with self._sem:
                with _TRACER.start_as_current_span("llm.slot_verify") as s:
                    oi.set_kind(s, oi.KIND_LLM)
                    s.set_attribute("verify.slot_name", slot_name)
                    s.set_attribute("verify.num_chunks", len(verifiable))
                    result = await self._llm.generate_messages(
                        [
                            ChatMessage(role="system", content=self._source.prompt_body),
                            ChatMessage(role="user", content=user_content),
                        ],
                        model_options=model_options,
                        grammar=GrammarSpec(kind="json_schema", value=self._source.schema),
                    )
                    content = result.text or "{}"
                    oi.set_llm_chat(
                        s,
                        model_name=result.model_id,
                        input_messages=[
                            ("system", self._source.prompt_body),
                            ("user", user_content),
                        ],
                        completion=content,
                        prompt_tokens=int(result.token_usage.get("prompt_tokens", 0)),
                        completion_tokens=int(result.token_usage.get("completion_tokens", 0)),
                    )
            parsed = self._parse_ids(content, all_ids)
        except Exception as exc:  # noqa: BLE001 — LLM 미가용/파싱 실패 → 슬롯 단위 degrade.
            # 그 슬롯 청크를 전량 necessary 로 보존(재검색 안 함). rationale 에 실패 사유를
            # 실어 러너가 UI thinking 에 "검증 호출 실패 → 전량 보존" 을 노출하게 한다.
            reason = f"{_FALLBACK_RATIONALE_PREFIX} ({type(exc).__name__}: {str(exc)[:200]})"
            return {
                "necessary_chunk_ids": list(all_ids),
                "multihop_chunk_ids": [],
                "rationale": reason[:_RATIONALE_CHARS_CAP],
                "method": "fallback",
            }

        return {
            "necessary_chunk_ids": parsed["necessary_chunk_ids"],
            "multihop_chunk_ids": parsed["multihop_chunk_ids"],
            "rationale": parsed["rationale"][:_RATIONALE_CHARS_CAP],
            "method": "llm",
        }

    def _render_user(self, query_text: str, answer_spec: str, slot_name: str,
                     slot_query: str, chunks: list[dict[str, Any]]) -> str:
        """슬롯 1개의 청크 전체를 한 user 메시지로 렌더. 각 청크는 `[chunk_id]` prefix
        (verify_slot_v2.md 형식) — 모델이 id 만 가리켜 판정한다. 청크 본문은
        `_CHUNK_RENDER_CHARS` 로 청크별 캡(슬롯 일괄이라 곱해지는 부피 가드)."""
        chunk_lines = []
        for c in chunks:
            cid = c.get("chunk_id")
            body = (c.get("text") or c.get("snippet") or "")[:_CHUNK_RENDER_CHARS]
            doc = c.get("document_id") or ""
            chunk_lines.append(f"- [{cid}] (doc={doc}) {body}")
        return "\n".join([
            f"USER QUESTION: {query_text}",
            "",
            "ANSWER SPEC:",
            answer_spec,
            "",
            f"SLOT: {slot_name}",
            f"SLOT SEARCH QUERY: {slot_query}",
            "",
            "RETRIEVED CHUNKS:",
            *chunk_lines,
        ])

    def _parse_ids(self, content: str, all_ids: list[str]) -> dict[str, Any]:
        """슬롯 모델 JSON → {necessary_chunk_ids, multihop_chunk_ids, rationale}. 두 id
        리스트는 입력 id 의 *부분집합*으로 필터한다(hallucinated id 제거, 입력 순서 보존 —
        계약: 두 집합 ⊆ 입력 chunk_id). 파싱 실패·형식 불일치는 raise(ValueError) 해
        호출부가 포착(슬롯 단위 fallback degrade)."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError("verify_slot JSON parse failed") from e
        if not isinstance(data, dict):
            raise ValueError("verify_slot output not an object")
        raw_necessary = {str(i) for i in (data.get("necessary_chunk_ids") or [])}
        raw_multihop = {str(i) for i in (data.get("multihop_chunk_ids") or [])}
        # 입력 순서 보존 + 부분집합 필터(입력 id 순회로 hallucinated id 제거·결정성 확보).
        necessary = [i for i in all_ids if i in raw_necessary]
        multihop = [i for i in all_ids if i in raw_multihop]
        return {
            "necessary_chunk_ids": necessary,
            "multihop_chunk_ids": multihop,
            "rationale": str(data.get("rationale", "")).strip(),
        }
