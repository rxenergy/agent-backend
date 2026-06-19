"""RescopePort 구현체 — :class:`~app.ports.llm.LLMPort`(Node2 vLLM)로 none_necessary
슬롯의 검색 스코프를 재계획한다(spec_driven_v2 retrieval.rescope = sub, verify/follow_up
과 같은 노드).

verify_slot 이 "이 슬롯 1차 검색 결과 전체가 빗나감"(none_necessary)으로 판정하면, 그
의견(why_not_needed/what_is_needed)과 1차 planning 스코프를 한 프롬프트로 합쳐 단일 LLM
호출(guided decoding, `GrammarSpec(kind="json_schema")`)로 재계획 쿼리를 산출한다. 모델은
planning 과 동일한 스코프 어휘(collection/status/design/canonical_id + boost/filter mode)를
내고, 결정형 게이트(`resolve_query_scope` — N2 QueryFormulator 와 공유)가 그것을 target/
filters 로 해소한다(드리프트 방지). 어댑터(HttpLLM)가 vLLM `guided_json` 으로 변환하므로 이
모듈은 외부 LLM SDK 를 직접 import 하지 않는다(원칙 #4).

동시 슬롯 호출 수는 인스턴스 소유 semaphore 로 캡한다 — verify·follow_up 과 같은 Node2 를
공유하므로 세 도구의 동시 호출이 같은 vLLM 예산(max_num_seqs)을 두고 경쟁한다. RetrievalRescope
Tool 의 도구 레벨 캡과 중복이나, 어댑터 경계의 안전판. LLM 미가용/파싱 실패는 슬롯 단위로
안전 degrade — method="fallback" + 빈 queries(러너가 재검색 skip → 단일노드 동작과 동형).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.application.intake.spec_driven_query import resolve_query_scope
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort

_TRACER = get_tracer("agent")

# 동시 슬롯 호출 수 상한 — 러너 fan-out 전체에 걸친 *전역* 캡(인스턴스 싱글톤). verify/
# follow_up 과 같은 Node2 vLLM 을 공유하므로 max_num_seqs 여유 안에서 잡되 큐 적체·타임아웃
# 캐스케이드를 막는다. 환경별 최적값은 SPEC_DRIVEN_MAX_QUERIES 로 함께 조정(profiles.py).
_MAX_CONCURRENCY = 48


class RescopeLlm:
    """RescopePort 구현체 — Node2 LLMPort 주입형(async, 슬롯당 단일 호출).

    `source` 는 registry 호스팅 rescope 프롬프트(prompt_body + json_schema +
    model_options). boot 시 sha 검증된 source 를 그대로 주입받는다(프롬프트 인라인 금지 —
    원칙 5). `max_concurrency` 는 동시 슬롯 호출 전역 캡."""

    def __init__(
        self, *, llm: LLMPort, source: Any, max_concurrency: int | None = None
    ) -> None:
        self._llm = llm
        self._source = source
        self._sem = asyncio.Semaphore(max_concurrency or _MAX_CONCURRENCY)

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
        model_options = dict(self._source.model_options or {})
        prompt = self._render(
            query_text, answer_spec, slot_name, slot_query,
            why_not_needed, what_is_needed, initial_scope, max_queries,
        )
        try:
            async with self._sem:
                with _TRACER.start_as_current_span("llm.rescope") as s:
                    oi.set_kind(s, oi.KIND_LLM)
                    s.set_attribute("rescope.slot_name", slot_name)
                    result = await self._llm.generate(
                        prompt,
                        model_options=model_options,
                        grammar=GrammarSpec(kind="json_schema", value=self._source.schema),
                    )
                    content = result.text or "{}"
                    oi.set_llm(
                        s,
                        model_name=result.model_id,
                        prompt=prompt,
                        completion=content,
                        prompt_tokens=int(result.token_usage.get("prompt_tokens", 0)),
                        completion_tokens=int(result.token_usage.get("completion_tokens", 0)),
                    )
            queries = self._parse(content, max_queries)
        except Exception:  # noqa: BLE001 — LLM 미가용/파싱 실패 → 슬롯 단위 degrade(재검색 skip).
            return {"queries": [], "method": "fallback"}

        return {"queries": queries, "method": "llm"}

    def _render(
        self, query_text: str, answer_spec: str, slot_name: str, slot_query: str,
        why_not_needed: str, what_is_needed: str, initial_scope: dict[str, Any],
        max_queries: int,
    ) -> str:
        """rescope 프롬프트 템플릿의 placeholder 를 치환(QueryFormulator 와 동형). 초기
        스코프는 사람이 읽는 JSON 한 줄로 직렬화해 "무엇이 실패했는지" 를 모델이 보게 한다."""
        try:
            scope_str = json.dumps(initial_scope, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            scope_str = str(initial_scope)
        return (
            self._source.prompt_body
            .replace("{query}", query_text)
            .replace("{spec}", answer_spec)
            .replace("{slot_name}", slot_name)
            .replace("{slot_query}", slot_query)
            .replace("{why_not_needed}", why_not_needed)
            .replace("{what_is_needed}", what_is_needed)
            .replace("{initial_scope}", scope_str)
            .replace("{max_queries}", str(max_queries))
        )

    def _parse(self, content: str, max_queries: int) -> list[dict[str, Any]]:
        """rescope 모델 JSON → [{query_text, target, filters, scope_audit}]. 스코프 채널
        해소는 `resolve_query_scope`(N2 QueryFormulator 와 공유)로 — collection/status/design/
        canonical_id 검증·배타성·정규화가 planning 과 *동일*하다. query_text 없는 항목은 skip,
        max_queries 로 절단. 파싱 실패·형식 불일치는 raise → 호출부가 포착(fallback degrade)."""
        text = (content or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            raise ValueError("rescope JSON not found")
        data = json.loads(text[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("rescope output not an object")
        raw = data.get("queries")
        if not isinstance(raw, list):
            raise ValueError("rescope output has no queries list")
        out: list[dict[str, Any]] = []
        for q in raw:
            if not isinstance(q, dict):
                continue
            qt = str(q.get("query_text") or "").strip()
            if not qt:
                continue
            target, filters, audit = resolve_query_scope(q)
            out.append({
                "query_text": qt,
                "target": target,
                "filters": filters,
                "scope_audit": audit,
            })
            if len(out) >= max_queries:
                break
        return out
