"""RefExtractorPort 구현체 — :class:`~app.ports.llm.LLMPort` 로 참조 추출 + 재검색
쿼리 생성.

app.adapters.ref_postprocess (chunking.postprocess 이식본)의 rule-base 해소 로직을
재사용한다. LLM 호출은 주입된 LLMPort(HttpLLM — spec_driven_v2 에선 sub 노드 =
SECONDARY_LLM)를 통해 수행하므로 이 어댑터는 외부 LLM SDK 를 직접 import 하지 않는다
(원칙 #4). catalog 파일은 생성 시점에 한 번만 로드된다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.adapters.ref_postprocess import (
    RefResolver,
    RefSettings,
    build_case_reference_index_from_catalog,
    build_report_number_index_from_catalog,
    load_or_build_catalog,
    resolve_text_with_follow_up,
)
from app.ports.llm import LLMPort


class LlmRefExtractor:
    """RefExtractorPort 구현체 — LLMPort 주입형(async)."""

    def __init__(
        self,
        llm: LLMPort,
        settings: RefSettings,
        catalog_csv_path: Path,
        cache_path: Path,
    ):
        # LLM 연결(endpoint/model/api_key/timeout·재시도)은 LLMPort 가 소유한다.
        # settings 는 추출 knob(max_output_tokens·schema 경로) 제공에만 쓰인다.
        self._llm = llm
        self._settings = settings
        catalog = load_or_build_catalog(
            csv_path=catalog_csv_path,
            cache_path=cache_path,
        )
        self._resolver = RefResolver(
            catalog,
            build_report_number_index_from_catalog(catalog),
            build_case_reference_index_from_catalog(catalog),
        )

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
        result = await resolve_text_with_follow_up(
            query_text=query_text,
            chunk_text=chunk_text,
            resolver=self._resolver,
            settings=self._settings,
            llm=self._llm,
            current_source_id=current_source_id,
            min_score=min_score,
            answer_spec=answer_spec,
            slot_query=slot_query,
            necessity_only=necessity_only,
        )
        return [
            {
                "query_text": fq.query_text,
                "target_source_ids": fq.target_source_ids,
                "intent": fq.intent,
            }
            for fq in result["follow_up_queries"]
        ]
