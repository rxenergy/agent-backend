from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort

_TRACER = get_tracer("intake")

# agentic_finder N0 — 질의 번역(Intake 경계). 워크플로우 *내부*(분류·답변사양·Finder
# 검색)는 영어 코퍼스(NRC RG/SRP/10 CFR, NuScale FSAR 등)를 대상으로 하므로 사용자
# 질의를 검색에 적합한 영어(`query_en`)로 한 번 번역하고, 동시에 사용자가 쓴 원래
# 언어(`source_language`)를 식별한다 — 최종 답변(N8)을 그 언어로 되돌리기 위함.
#
# 룰 사상표가 아니라 *모델*(utility LLM + json_schema grammar, temperature 0)이
# 산출한다(표현=모델, [[feedback_model_over_rule]]). 실패(파싱불가/미가용) 시에만
# 결정론 fallback(원문 passthrough)으로 강등하고 어느 경로였는지 `method` 로 기록한다
# (silent degrade 금지). 프롬프트·스키마·model_options 는 코드 인라인이 아니라
# prompts/registry.yaml 의 query_translate_prompts 블록에서 관리되며
# QueryTranslatePromptSource 가 sha 검증 후 주입한다(분류/answer_spec 과 동형).
#
# ⚠️ 번역은 워크플로우 상태다 — 재현하려면 InteractionEvent 가 (원질의, query_en,
# source_language, method, policy_hash)를 핀해야 한다(원칙 5). 호출부가
# `policy_hash`/method 를 query_understanding 으로 넘긴다.

# method="fallback"(모델 부재/실패) 시 답변 언어 기본값. 이 에이전트의 1차 사용자는
# 한국어 사용자이므로(프로젝트 도메인) 원문 언어를 못 읽으면 Korean 으로 답한다.
_FALLBACK_LANGUAGE = "Korean"


@dataclass(frozen=True)
class TranslatedQuery:
    """N0 산출물. `query_en` = 내부 영어 질의, `source_language` = 최종 답변 언어."""

    query_en: str
    source_language: str
    instantiation_method: str  # "llm" | "fallback"
    policy_hash: str | None = None


class QueryTranslator:
    """N0 — 사용자 질의를 내부 영어 질의로 번역하고 원 언어를 식별. 프롬프트·스키마는
    registry 에서 주입(QueryTranslatePromptSource.build_translator)."""

    version = "query_translate/v1"

    def __init__(
        self,
        llm: LLMPort,
        *,
        prompt_body: str,
        schema: dict | None = None,
        model_options: dict | None = None,
        policy_hash: str | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt_body
        self._schema = schema
        self._model_options = dict(model_options or {"temperature": 0.0})
        self._policy_hash = policy_hash

    async def translate(self, query_text: str) -> TranslatedQuery:
        # .replace (not .format): 프롬프트 본문에 JSON 예시의 { } 가 있어 .format 은
        # KeyError. AnswerSpecInstantiator 와 동일 idiom.
        prompt = self._prompt.replace("{query}", query_text)
        with _TRACER.start_as_current_span("intake.query_translate") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            if self._policy_hash:
                span.set_attribute("query_translate.policy_hash", self._policy_hash)
            try:
                grammar = (
                    GrammarSpec(kind="json_schema", value=self._schema)
                    if self._schema else None
                )
                res = await self._llm.generate(
                    prompt, model_options=dict(self._model_options), grammar=grammar,
                )
                oi.set_llm(
                    span, model_name=res.model_id, prompt=prompt, completion=res.text,
                    prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(res.token_usage.get("completion_tokens", 0)),
                )
                parsed = _parse(res.text)
                if parsed is not None and parsed[0]:
                    query_en, source_language = parsed
                    span.set_attribute("query_translate.method", "llm")
                    span.set_attribute("query_translate.source_language", source_language)
                    oi.set_io(span, output_value={
                        "method": "llm", "source_language": source_language,
                        "query_en": query_en,
                    })
                    return TranslatedQuery(
                        query_en=query_en, source_language=source_language,
                        instantiation_method="llm", policy_hash=self._policy_hash,
                    )
            except Exception:  # noqa: BLE001 — 미가용/파싱불가 → 결정론 fallback
                pass
            # fallback — 원문 passthrough(번역 없음). 검색 품질은 떨어지지만 워크플로우는
            # 막지 않는다(graceful degrade). 답변 언어는 원문을 못 읽으므로 기본값.
            span.set_attribute("query_translate.method", "fallback")
            oi.set_io(span, output_value={"method": "fallback"})
            return TranslatedQuery(
                query_en=query_text, source_language=_FALLBACK_LANGUAGE,
                instantiation_method="fallback", policy_hash=self._policy_hash,
            )


def _parse(text: str) -> tuple[str, str] | None:
    text = (text or "").strip()
    # grammar 미적용 백엔드가 코드펜스·서두를 붙일 수 있어 관대하게 추출.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    query_en = str(data.get("query_en") or "").strip()
    source_language = str(data.get("source_language") or "").strip() or _FALLBACK_LANGUAGE
    if not query_en:
        return None
    return query_en, source_language


def translate_policy_hash(prompt_body: str) -> str:
    """프롬프트 본문 sha16 — InteractionEvent query_translate 핀과 동일 idiom."""
    return hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]
