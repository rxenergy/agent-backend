"""SlotVerifierPort 구현체 — :class:`~app.ports.llm.LLMPort`(Node2 vLLM)로 슬롯 1개의
1차 검색 결과를 검증한다(spec_driven_v2 Node2).

검증은 **청크별로 독립 LLM 호출**을 띄워 `asyncio.gather` 로 동시 발사한다(continuous batch
— retrieval.follow_up 과 동형). 각 호출은 청크 1개를 `{necessary, multihop}` boolean 으로
판정하고(guided decoding, `GrammarSpec(kind="json_schema")`), 어댑터가 boolean 을 모아
necessary/multihop 식별자 두 집합으로 집계한다. 어댑터(HttpLLM)가 vLLM `guided_json` 으로
변환하므로 이 모듈은 외부 LLM SDK 를 직접 import 하지 않는다(원칙 #4).

동시 청크 호출 수는 인스턴스 소유 semaphore 로 캡한다 — 슬롯들이 동시에 verify 를 돌려도
(러너 fan-out) Node2 vLLM 에 나가는 청크 호출 총량을 전역으로 묶는다(`max_num_seqs` 초과
발사 → 큐 적체·타임아웃 캐스케이드 방지).

출력은 청크 *식별자*다(address-not-content 불변): 답변에 꼭 필요한 청크 + 멀티홉(재검색)
필요 청크. LLM 미가용/파싱 실패는 청크별로 안전 degrade — 그 청크는 necessary 로 보존하고
멀티홉에서 제외한다(전량 보존·재검색 안 함 → 단일노드 동작과 동형). 전 청크 실패 시
method="fallback"(necessary=전량, multihop=없음)으로 기존 계약과 byte-identical.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import ChatMessage, GrammarSpec, LLMPort, LLMUnavailableError

_TRACER = get_tracer("agent")

# 청크 본문 렌더 캡(char). verify 는 "필요/멀티홉 판정"만 하므로 전문이 필요 없다 —
# snippet 길이로 충분하고, Node2 KV-cache·디코딩 비용을 줄인다(공유 vLLM 안전판).
_CHUNK_RENDER_CHARS = 1200

# 청크별 동시 호출 수 상한 — 슬롯 fan-out 전체에 걸친 *전역* 캡(인스턴스 싱글톤이라 모든
# 슬롯이 self._sem 을 공유). 청크별 호출은 독립 HTTP 요청이라 동시에 쏘면 Node2 vLLM 의
# continuous batching 이 한 GPU step 에 묶는다 — 이 batching 을 살리도록 캡을
# VLLM_MAX_NUM_SEQS 여유 안에서 넉넉히 둔다. 너무 높이면 큐 적체 → per-call 타임아웃·재시도
# 캐스케이드. 환경별 최적값은 SPEC_DRIVEN_V2_VERIFY_CHUNK_CONCURRENCY 로 조정한다(profiles.py).
_MAX_CONCURRENCY = 48

# 집계 rationale 길이 가드 — 청크별 rationale 을 줄바꿈 결합하므로 청크가 많으면 길어진다.
# UI thinking 노출용이라 과도한 bloat 만 막는다(총 char cap).
_RATIONALE_CHARS_CAP = 4000


class SlotVerifierLlm:
    """SlotVerifierPort 구현체 — Node2 LLMPort 주입형(async, 청크별 fan-out).

    `source` 는 registry 호스팅 verify(chunk) 프롬프트(prompt_body + json_schema +
    model_options). boot 시 sha 검증된 source 를 그대로 주입받는다(프롬프트 인라인 금지 —
    원칙 5). `max_concurrency` 는 청크별 동시 호출 전역 캡."""

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

        async def _verify_one(chunk: dict[str, Any]) -> dict[str, Any]:
            """청크 1개 판정. LLMUnavailableError·복구불가 파싱은 raise 해 gather 가 포착
            (해당 청크는 호출부에서 necessary 보존 degrade)."""
            cid = str(chunk.get("chunk_id"))
            user_content = self._render_user_one(
                query_text, answer_spec, slot_name, slot_query, chunk
            )
            async with self._sem:
                with _TRACER.start_as_current_span("llm.chunk_verify") as s:
                    oi.set_kind(s, oi.KIND_LLM)
                    s.set_attribute("verify.slot_name", slot_name)
                    s.set_attribute("verify.chunk_id", cid)
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
            return self._parse_one(cid, content)

        results = await asyncio.gather(
            *(_verify_one(c) for c in verifiable),
            return_exceptions=True,
        )

        necessary_ids: list[str] = []
        multihop_ids: list[str] = []
        rationale_lines: list[str] = []
        any_ok = False
        for chunk, res in zip(verifiable, results):
            cid = str(chunk.get("chunk_id"))
            if isinstance(res, BaseException):
                # 한 청크 실패가 슬롯 전체를 죽이지 않게: 그 청크는 necessary 로 보존(전량
                # 보존 degrade), 멀티홉 제외. method 는 다른 청크 성공 여부로 결정.
                necessary_ids.append(cid)
                continue
            any_ok = True
            if res["necessary"]:
                necessary_ids.append(cid)
            if res["multihop"]:
                multihop_ids.append(cid)
            if res["rationale"]:
                tag = "필요" if res["necessary"] else ("멀티홉" if res["multihop"] else "제외")
                rationale_lines.append(f"[{cid}] ({tag}) {res['rationale']}")

        # 전 청크 실패 → 기존 fallback 계약과 byte-identical(necessary=전량, multihop=없음).
        method = "llm" if any_ok else "fallback"
        rationale = "\n".join(rationale_lines)[:_RATIONALE_CHARS_CAP]
        return {
            "necessary_chunk_ids": necessary_ids,
            "multihop_chunk_ids": multihop_ids,
            "rationale": rationale,
            "method": method,
        }

    def _render_user_one(self, query_text: str, answer_spec: str, slot_name: str,
                         slot_query: str, chunk: dict[str, Any]) -> str:
        cid = chunk.get("chunk_id")
        body = (chunk.get("text") or chunk.get("snippet") or "")[:_CHUNK_RENDER_CHARS]
        doc = chunk.get("document_id") or ""
        return "\n".join([
            f"USER QUESTION: {query_text}",
            "",
            "ANSWER SPEC:",
            answer_spec,
            "",
            f"SLOT: {slot_name}",
            f"SLOT SEARCH QUERY: {slot_query}",
            "",
            "THE CHUNK:",
            f"- [{cid}] (doc={doc}) {body}",
        ])

    def _parse_one(self, cid: str, content: str) -> dict[str, Any]:
        """청크 1개 모델 JSON → {chunk_id, necessary, multihop, rationale}. 파싱 실패·형식
        불일치는 raise(ValueError) 해 gather 가 포착(호출부에서 necessary 보존 degrade)."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"verify_chunk JSON parse failed for {cid}") from e
        if not isinstance(data, dict):
            raise ValueError(f"verify_chunk output not an object for {cid}")
        return {
            "chunk_id": cid,
            "necessary": bool(data.get("necessary", False)),
            "multihop": bool(data.get("multihop", False)),
            "rationale": str(data.get("rationale", "")).strip(),
        }
