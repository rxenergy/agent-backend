from __future__ import annotations

import json
import re
from typing import Iterable

from app.application.classification.rule import _extract_entities
from app.domain.classification import (
    DEFAULT_DEPTH,
    DEFAULT_OBJECT,
    ClassificationResult,
)
from app.domain.interaction import ChatTurn
from app.ports.llm import LLMPort, LLMUnavailableError

_PROMPT = """\
너는 SMR 인허가 도메인 질의 분류기다. 사용자 질의를 (Object, Depth)로 분류한다.

Object:
- O1 Vendor: 특정 노형의 기술/설계/실험
- O2 Regulation: NRC/KINS 규제/법령 조항
- O3 RAI: RAI 또는 NRC 심사 기록
- O4 Relation: 객체 간 관계 (노형↔규제, RAI↔규제 등)

Depth:
- D1 Overview: 현황/통계/패턴
- D2 Technical: 기술 디테일/메커니즘/수치/인과 사슬
- D3 Formal: 원문/정의/조항/공식 요건

응답은 다음 JSON 형식으로만 답한다(설명 금지):
{"object":"O1|O2|O3|O4","depth":"D1|D2|D3","object_confidence":0.0-1.0,"depth_confidence":0.0-1.0}

질의: {query}
"""

_RE_JSON = re.compile(r"\{[^{}]*\}", re.S)


class LLMClassifier:
    backend = "llm"

    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    async def classify(
        self,
        query_text: str,
        chat_history: Iterable[ChatTurn] = (),
    ) -> ClassificationResult:
        prompt = _PROMPT.replace("{query}", query_text)
        try:
            result = await self._llm.generate(
                prompt, model_options={"temperature": 0.0, "max_tokens": 100}
            )
        except LLMUnavailableError:
            return ClassificationResult(
                scenario_object=DEFAULT_OBJECT,
                scenario_depth=DEFAULT_DEPTH,
                entities=_extract_entities(query_text),
                confidence=0.0,
                low_confidence_reason="llm_classifier_unavailable",
                classifier_backend=self.backend,
            )
        parsed = _parse_json(result.text)
        if parsed is None:
            return ClassificationResult(
                scenario_object=DEFAULT_OBJECT,
                scenario_depth=DEFAULT_DEPTH,
                entities=_extract_entities(query_text),
                confidence=0.0,
                low_confidence_reason="llm_classifier_parse_failed",
                classifier_backend=self.backend,
            )
        obj = str(parsed.get("object", DEFAULT_OBJECT))
        dep = str(parsed.get("depth", DEFAULT_DEPTH))
        oc = float(parsed.get("object_confidence", 0.0) or 0.0)
        dc = float(parsed.get("depth_confidence", 0.0) or 0.0)
        if obj not in ("O1", "O2", "O3", "O4"):
            obj = DEFAULT_OBJECT
            oc = 0.0
        if dep not in ("D1", "D2", "D3"):
            dep = DEFAULT_DEPTH
            dc = 0.0
        return ClassificationResult(
            scenario_object=obj,
            scenario_depth=dep,
            entities=_extract_entities(query_text),
            confidence=round(min(oc, dc), 3),
            object_confidence=round(oc, 3),
            depth_confidence=round(dc, 3),
            classifier_backend=self.backend,
        )


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _RE_JSON.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
