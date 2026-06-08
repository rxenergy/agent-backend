from __future__ import annotations

from app.application.context.pack import ContextBuilder
from app.domain.retrieval import RetrievedChunk

# W-D/W-E 소비 검증 — 규범무게 태그가 *생성 시점 CONTEXT* 에 닿는지(formatted 가
# render_for_prompt 의 per-chunk head). system_v6 Citation Format 3 / 제3원칙이
# "CONTEXT 에 주어진 그대로" 태그 보존을 전제하므로 load-bearing.


def _chunk(**kw) -> RetrievedChunk:
    base = dict(chunk_id="ch", document_id="doc", score=0.9, snippet="근거 문장")
    base.update(kw)
    return RetrievedChunk(**base)


def _render(chunks) -> str:
    b = ContextBuilder(capture_mode="snippets")
    pack = b.build(
        interaction_id="i", query_text="q", chat_history=(),
        conversation_summary=None, scenario_object="O2", scenario_depth="D2",
        entities={}, chunks=chunks,
    )
    return b.render_for_prompt(pack)


def test_weight_tag_present_in_generation_context() -> None:
    # 모델이 본문을 쓰는 동안 보는 CONTEXT 의 cite 줄에 무게가 실려야 한다.
    ctx = _render([
        _chunk(chunk_id="c0", document_id="ML17005B456", section="50.46", page=1,
               doc_type="10CFR", collection="10CFR", clause_id="10CFR50.46"),
        _chunk(chunk_id="c1", document_id="ML18001A123", section="C.1", page=4,
               doc_type="RG", collection="RG", clause_id="RG_1_157"),
    ])
    # 같은 cite 줄(head)에 무게 태그가 동반 — 구속 vs 권고 분리.
    assert "[cite-0]" in ctx and "(구속 요건)" in ctx
    assert "[cite-1]" in ctx and "(권고·비구속 지침)" in ctx
    # 태그가 head 줄에 있고(스니펫과 같은 항목), 본문도 함께 실린다.
    assert "근거 문장" in ctx
