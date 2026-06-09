from __future__ import annotations

from app.application.agents.react_loop import REACT_ECHO_TOOL_SPECS
from app.application.agents.react_minimal_v1 import ReactMinimalRunner
from app.application.agents.registry import AgentDeps, register_variant
from app.domain.agents import VariantSpec

REACT_ECHO_VARIANT_ID = "react_echo_v1"


class ReactEchoRunner(ReactMinimalRunner):
    """react_echo_v1 — react_minimal_v1 의 **도구-최소(echo)** 변형.

    독립 변수는 단 둘이다: (1) Phase 1 ReAct 루프의 도구 세트 = retrieval.search +
    submit_response 만(REACT_ECHO_TOOL_SPECS — confidence.scope·terminology.*·
    retrieval.scope 제거), (2) N1 Retrieval 시스템 프롬프트(react_retrieval_echo_v1 —
    키워드 보존형). 그 외 harness(ReAct 루프 mechanics, Phase 2 Generation, 관측 전용
    검증, 이벤트 발행, 거부 1급)는 ReactMinimalRunner 에서 그대로 상속한다 — 측정되는
    차이가 코드 drift 가 아니라 *도구·프롬프트* 에만 귀속되도록(실험 변수 격리).

    설계 의도: 원자력 같은 전문 도메인에서 검색 품질을 가르는 것은 키워드 충실도다.
    정규화/확장 도구가 도메인 키워드를 치환·소실시키는 대신, 모델이 질의 원문의 키워드
    (노형명·규제 ID·RAI 번호·기술 약어)를 보존한 query_text 를 *추론만으로* 작성한다.
    scope/거부 판정은 도구 신호 없이 submit_response.outcome 으로 표현한다(표현=모델 /
    결정=코드). confidence.scope 부재로 term_coverage·corpus_map_hash·scope_mode 재현
    핀은 None 이 되며, 상속한 recorder/refuse 경로가 이를 그대로 수용한다."""

    _tool_specs = REACT_ECHO_TOOL_SPECS


@register_variant(REACT_ECHO_VARIANT_ID)
def _build_react_echo(spec: VariantSpec, deps: AgentDeps) -> "ReactEchoRunner":
    t = deps.tunables
    return ReactEchoRunner(
        spec=spec,
        llm_router=deps.llm_router,
        tool_executor=deps.tool_executor,
        context_builder=deps.context_builder,
        recorder=deps.recorder,
        event_sink=deps.event_sink,
        app_profile=deps.app_profile,
        utility_llm=deps.utility_llm,
        # N1 은 echo 전용 키워드-보존 프롬프트, N2 Generation 은 react_minimal 과 공유.
        react_retrieval_source=deps.react_echo_retrieval_prompt_source,
        react_generation_source=deps.react_generation_prompt_source,
        citation_contract_path=t.get("citation_contract_path"),
        react_max_turns=t.get("react_max_turns", 8),
        verification_citation_threshold=t.get("verification_citation_threshold", 0.9),
        verification_faithfulness_threshold=t.get("verification_faithfulness_threshold", 0.85),
    )
