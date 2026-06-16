from __future__ import annotations

from app.api.openai_compat import _smr_agent_metadata
from app.domain.interaction import AgentResponse, Citation


def _resp(**kw) -> AgentResponse:
    base = dict(
        interaction_id="i1",
        answer_text="ans",
        citations=(),
        refusal_reason=None,
        verification_status="pass",
        scenario_object="O2",
        scenario_depth="D2",
        latency_ms=10,
    )
    base.update(kw)
    return AgentResponse(**base)


def test_regulatory_grounding_exposed_in_custom_field():
    """안전 계약: 구조화 클라이언트는 verification_status 와 *나란히*
    regulatory_grounding 을 봐야 v1 미검증 PASS 를 검증된 답으로 오인하지 않는다."""
    meta = _smr_agent_metadata(
        interaction_id="i1", runner_variant="hierarchical_corrective_v3_1",
        resolved_llm="x", response=_resp(verification_status="pass",
                                         regulatory_grounding="unverified"),
    )
    assert meta["verification_status"] == "pass"
    assert meta["regulatory_grounding"] == "unverified"


def test_regulatory_grounding_defaults_na_for_other_variants():
    # v2 응답(기본 n_a)도 안전하게 노출.
    meta = _smr_agent_metadata(
        interaction_id="i1", runner_variant="agentic_finder_v4",
        resolved_llm="x", response=_resp(),
    )
    assert meta["regulatory_grounding"] == "n_a"


def test_citations_expose_source_url_and_tables():
    # 구조화 소비자(eval/감사)용 — citation 의 source_url·tables 원본이 smr_agent 에
    # 노출된다(원칙 8, spec_driven_table_citation_references D7). OpenWebUI 는 무시하나
    # content 의 References 가 사람용 렌더를 담당.
    tables = [{"tag": "t", "caption": "C", "markdown": "| a |", "html": ""}]
    cite = Citation(citation_id="cite-1", document_id="ML18002A422",
                    source_url="https://www.nrc.gov/docs/ML1800/ML18002A422.pdf",
                    tables=tables, kind="table", table_tag="t")
    meta = _smr_agent_metadata(
        interaction_id="i1", runner_variant="spec_driven_v1",
        resolved_llm="x", response=_resp(citations=(cite,)),
    )
    c0 = meta["citations"][0]
    assert c0["source_url"] == "https://www.nrc.gov/docs/ML1800/ML18002A422.pdf"
    assert c0["tables"] == tables
    # 입도 구분(kind/table_tag)도 구조화 소비자에 노출(table 근거 분리 집계).
    assert c0["kind"] == "table"
    assert c0["table_tag"] == "t"
