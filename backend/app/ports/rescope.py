from __future__ import annotations

from typing import Any, Protocol


class RescopePort(Protocol):
    """none_necessary 슬롯의 검색 스코프를 재계획하는 포트(spec_driven_v2 retrieval.rescope).

    verify_slot 이 "이 슬롯 1차 검색 결과 전체가 빗나감"(none_necessary)으로 판정하면,
    그 의견(why_not_needed/what_is_needed)과 1차 planning 스코프(initial_scope)를 받아
    planning 단계와 동일한 스코프 어휘(collection/status/design/canonical_id + boost/filter
    mode)로 검색 스코프를 새로 잡는다. application 계층은 이 Protocol 에만 의존하고,
    vLLM 구현체는 adapters 에 둔다(원칙 #4)."""

    async def rescope(
        self,
        *,
        query_text: str,
        answer_spec: str,
        slot_name: str,
        slot_query: str,
        why_not_needed: str,
        what_is_needed: str,
        initial_scope: dict[str, Any],
        max_queries: int = 3,
    ) -> dict[str, Any]:
        """반환: {"queries": [{"query_text": str, "target": {field:[str]},
        "filters": {field:...}, "scope_audit": {...}}], "method": "llm"|"fallback"}.

        queries 는 planning(FormulatedQuery)과 동일 표현 — query_text + 결정형 게이트로
        해소된 target(boost)/filters(hard-scope). LLM 미가용/파싱 실패 → method="fallback",
        빈 queries(러너가 재검색 skip)."""
        ...
