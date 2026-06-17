"""SlotVerifierPort 구현체 — :class:`~app.ports.llm.LLMPort`(Node2 vLLM)로 슬롯 1개의
1차 검색 결과를 검증한다(spec_driven_v2 Node2).

LLM 호출은 주입된 LLMPort(메인 경로와 동일한 HttpLLM, 단 Node2=SECONDARY_LLM 엔드포인트)를
통해 `generate_messages` + `GrammarSpec(kind="json_schema")` guided decoding 으로 1회만
수행한다. 어댑터(HttpLLM)가 vLLM `guided_json` 으로 변환하므로 이 모듈은 외부 LLM SDK 를
직접 import 하지 않는다(원칙 #4).

출력은 청크 *식별자*다(address-not-content 불변): 답변에 꼭 필요한 청크 + 멀티홉(재검색)
필요 청크. 두 집합은 입력 chunk_id 로 하드 필터된다(모델이 만든 가짜 id 차단). LLM 미가용/
파싱 실패 시 안전 degrade — method="fallback", necessary=전체, multihop=없음(전량 보존·
재검색 안 함 → 단일노드 동작과 동형).
"""

from __future__ import annotations

import json
from typing import Any

from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import ChatMessage, GrammarSpec, LLMPort, LLMUnavailableError

_TRACER = get_tracer("agent")

# 청크 본문 렌더 캡(char). verify 는 "필요/멀티홉 판정"만 하므로 전문이 필요 없다 —
# snippet 길이로 충분하고, Node2 KV-cache·디코딩 비용을 줄인다(공유 vLLM 안전판).
_CHUNK_RENDER_CHARS = 1200


class SlotVerifierLlm:
    """SlotVerifierPort 구현체 — Node2 LLMPort 주입형(async).

    `source` 는 registry 호스팅 verify 프롬프트(prompt_body + json_schema + model_options).
    boot 시 sha 검증된 source 를 그대로 주입받는다(프롬프트 인라인 금지 — 원칙 5)."""

    def __init__(self, *, llm: LLMPort, source: Any) -> None:
        self._llm = llm
        self._source = source

    async def verify_slot(
        self,
        *,
        query_text: str,
        answer_spec: str,
        slot_name: str,
        slot_query: str,
        chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # 입력 chunk_id 집합 — 출력 하드 필터(모델 환각 id 차단)와 fallback 전량 보존의 기준.
        all_ids = [str(c.get("chunk_id")) for c in chunks if c.get("chunk_id")]
        if not all_ids:
            # 빈 입력은 호출 낭비 — 러너가 0건 슬롯을 스킵하지만 방어적으로 한 번 더.
            return {"necessary_chunk_ids": [], "multihop_chunk_ids": [],
                    "rationale": "", "method": "fallback"}

        user_content = self._render_user(query_text, answer_spec, slot_name,
                                         slot_query, chunks)
        model_options = dict(self._source.model_options or {})
        try:
            with _TRACER.start_as_current_span("llm.slot_verify") as s:
                oi.set_kind(s, oi.KIND_LLM)
                s.set_attribute("verify.slot_name", slot_name)
                s.set_attribute("verify.num_chunks", len(all_ids))
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
        except LLMUnavailableError:
            # Node2 미가용 — 안전 degrade(전량 necessary, 멀티홉 없음). 러너가 단일노드처럼.
            return {"necessary_chunk_ids": list(all_ids), "multihop_chunk_ids": [],
                    "rationale": "", "method": "fallback"}

        return self._parse(content, all_ids)

    def _render_user(self, query_text: str, answer_spec: str, slot_name: str,
                     slot_query: str, chunks: list[dict[str, Any]]) -> str:
        lines = [
            f"USER QUESTION: {query_text}",
            "",
            "ANSWER SPEC:",
            answer_spec,
            "",
            f"SLOT: {slot_name}",
            f"SLOT SEARCH QUERY: {slot_query}",
            "",
            "RETRIEVED CHUNKS (first pass):",
        ]
        for c in chunks:
            cid = c.get("chunk_id")
            body = (c.get("text") or c.get("snippet") or "")[:_CHUNK_RENDER_CHARS]
            doc = c.get("document_id") or ""
            lines.append(f"- [{cid}] (doc={doc}) {body}")
        return "\n".join(lines)

    def _parse(self, content: str, all_ids: list[str]) -> dict[str, Any]:
        """모델 JSON → 식별자 두 집합. 파싱 실패 시 fallback(전량 necessary). 출력 id 는
        입력 집합으로 하드 필터(환각 id 제거), 멀티홉 ⊆ necessary 강제는 하지 않는다
        (멀티홉이지만 그 자체로는 불필요한 청크가 있을 수 있음 — 모델 판정 존중)."""
        valid = set(all_ids)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {"necessary_chunk_ids": list(all_ids), "multihop_chunk_ids": [],
                    "rationale": "", "method": "fallback"}
        if not isinstance(data, dict):
            return {"necessary_chunk_ids": list(all_ids), "multihop_chunk_ids": [],
                    "rationale": "", "method": "fallback"}

        def _ids(key: str) -> list[str]:
            raw = data.get(key) or []
            if not isinstance(raw, list):
                return []
            out: list[str] = []
            seen: set[str] = set()
            for x in raw:
                sid = str(x).strip()
                if sid in valid and sid not in seen:
                    seen.add(sid)
                    out.append(sid)
            return out

        return {
            "necessary_chunk_ids": _ids("necessary_chunk_ids"),
            "multihop_chunk_ids": _ids("multihop_chunk_ids"),
            "rationale": str(data.get("rationale", "")).strip(),
            "method": "llm",
        }
