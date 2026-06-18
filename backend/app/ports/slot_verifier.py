from __future__ import annotations

from typing import Any, Protocol


class SlotVerifierPort(Protocol):
    """슬롯 1개의 1차 검색 결과를 "사용자 질문 + answer_spec + 검색 쿼리" 기준으로
    검증하는 포트(spec_driven_v2 Node2). application 계층은 이 Protocol 에만 의존하고,
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
        "multihop_search_directions": {chunk_id: str}, "neighbor_requests":
        {chunk_id: "before"|"after"|"both"}, "rationale": str,
        "method": "llm"|"fallback"}.

        necessary/multihop id 집합 ⊆ 입력 chunk_id. multihop_search_directions 키 ⊆
        multihop_chunk_ids(그 외부 문서를 어느 방향으로 재검색할지 1문장). neighbor_requests
        키 ⊆ necessary_chunk_ids(앞/뒤 문맥 보강 필요 청크와 방향). rationale 은 LLM 산출이
        아니라 구현체가 구조화 필드에서 합성한 요약(핀·UI 연속성)."""
        ...
