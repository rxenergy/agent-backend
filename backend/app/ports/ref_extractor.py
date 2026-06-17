from __future__ import annotations

from typing import Any, Protocol


class RefExtractorPort(Protocol):
    """청크 텍스트에서 외부 참조를 추출하고 재검색 쿼리를 생성하는 포트.

    application 계층은 이 Protocol에만 의존한다.
    vLLM/Gemma-4 구현체는 adapters에 위치한다.
    """

    async def extract_follow_ups(
        self,
        query_text: str,
        chunk_text: str,
        current_source_id: str | None = None,
        min_score: float = 0.6,
        answer_spec: str | None = None,
        slot_query: str | None = None,
        necessity_only: bool = False,
    ) -> list[dict[str, Any]]:
        """단일 청크에서 follow-up 쿼리를 추출.

        `answer_spec`/`slot_query`/`necessity_only` 는 spec_driven_v2 N3.5 고도화용
        (옵셔널 — 미지정 시 기존 동작). `necessity_only=True` 면 청크의 모든 외부 참조가
        아니라 answer_spec+slot_query 기준 "답변에 꼭 필요한" 참조만 선별한다.

        반환: [{"query_text": str, "target_source_ids": [str], "intent": str}, ...]
        """
        ...
