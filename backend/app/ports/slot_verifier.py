from __future__ import annotations

from typing import Any, Protocol


class SlotVerifierPort(Protocol):
    """슬롯 1개의 1차 검색 결과를 "사용자 질문 + answer_spec + 검색 쿼리" 기준으로
    검증하는 포트(spec_driven_v2 Node2). application 계층은 이 Protocol 에만 의존하고,
    vLLM/Gemma-4 구현체는 adapters 에 둔다(원칙 #4).

    출력은 청크 *식별자*다(address-not-content 불변): 답변에 꼭 필요한 청크와 멀티홉
    (추가 재검색) 이 필요한 청크를 가리킬 뿐, 내용을 옮기지 않는다.

    BINARY verdict(has_necessary/none_necessary)를 함께 낸다. none_necessary 면 검색
    결과 전체가 빗나가 쓸 청크가 없다는 판정 — 청크 식별자 대신 단일 opinion
    (why_not_needed/what_is_needed)을 내고, 러너가 retrieval.rescope 로 스코프를 재계획한다."""

    async def verify_slot(
        self,
        *,
        query_text: str,
        answer_spec: str,
        slot_name: str,
        slot_query: str,
        chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """반환: {"verdict": "has_necessary"|"none_necessary", "opinion":
        {"why_not_needed": str, "what_is_needed": str}, "necessary_chunk_ids": [str],
        "multihop_chunk_ids": [str], "multihop_search_directions": {chunk_id: str},
        "neighbor_requests": {chunk_id: "before"|"after"|"both"}, "rationale": str,
        "method": "llm"|"fallback"}.

        verdict=none_necessary 면 청크 식별자 집합은 모두 비고 opinion 이 채워진다(러너가
        rescope 재검색 트리거). has_necessary 면 opinion 은 빈 dict. necessary/multihop id
        집합 ⊆ 입력 chunk_id. multihop_search_directions 키 ⊆ multihop_chunk_ids(그 외부
        문서를 어느 방향으로 재검색할지 1문장). neighbor_requests 키 ⊆ necessary_chunk_ids
        (앞/뒤 문맥 보강 필요 청크와 방향). rationale 은 LLM 산출이 아니라 구현체가 구조화
        필드에서 합성한 요약(핀·UI 연속성). fallback 은 항상 has_necessary(전량 보존·재검색 안 함)."""
        ...
