from __future__ import annotations

from typing import Any, Protocol


class SlotVerifierPort(Protocol):
    """슬롯 1개의 1차 검색 결과를 "사용자 질문 + answer_spec + 검색 쿼리" 기준으로
    검증하는 포트(spec_driven_v2 Node1). application 계층은 이 Protocol 에만 의존하고,
    vLLM/Gemma-4 구현체는 adapters 에 둔다(원칙 #4).

    출력은 청크 *식별자*다(address-not-content 불변): 답변에 꼭 필요한 청크와 멀티홉
    (추가 재검색) 이 필요한 청크를 가리킬 뿐, 내용을 옮기지 않는다."""

    async def verify_slot(
        self,
        *,
        query_text: str,
        answer_spec: str,
        slot_name: str,
        slot_query: str,
        chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """반환: {"necessary_chunk_ids": [str], "multihop_chunk_ids": [str],
        "rationale": str, "method": "llm"|"fallback"}. 두 id 집합 ⊆ 입력 chunk_id."""
        ...
