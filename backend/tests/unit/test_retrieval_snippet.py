from __future__ import annotations

from app.application.retrieval.snippet import SnippetExtractor, regex_sentence_split
from app.domain.retrieval import RetrievedChunk


def _chunk(cid: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=cid, document_id="d", score=0.8, snippet=text,
                          section="§1", page=3, revision="rev2")


def test_sentence_split_handles_ko_en_punctuation():
    s = regex_sentence_split("First sentence. 두 번째 문장이다. Third? 네 번째!")
    assert s == ["First sentence.", "두 번째 문장이다.", "Third?", "네 번째!"]


def test_window_picks_entity_dense_sentence():
    ex = SnippetExtractor(window=1, max_sentences=3)
    text = (
        "Intro paragraph with no entities. "
        "The i-SMR ECCS uses passive cooling. "
        "Unrelated trailing remark."
    )
    pack = ex.extract(
        [_chunk("c1", text)],
        query_text="i-SMR ECCS passive cooling",
        entities={"reactor_type": ["i-SMR"], "phenomenon": ["ECCS"]},
    )
    win = pack.snippets[0].text
    # The entity-dense middle sentence must be in the window.
    assert "i-SMR ECCS uses passive cooling" in win
    # window=1 around the best (middle) sentence → includes neighbours, ≤3 sentences.
    assert win.count(".") <= 3


def test_snippet_carries_citation_and_metadata():
    ex = SnippetExtractor()
    pack = ex.extract(
        [_chunk("c1", "i-SMR ECCS design.")],
        query_text="i-SMR ECCS", entities={}, citation_ids=["cite-0"],
    )
    s = pack.snippets[0]
    assert s.citation_id == "cite-0"
    assert s.chunk_id == "c1"
    assert s.section == "§1" and s.page == 3 and s.revision == "rev2"
    assert s.snippet_id == "c1#s0"


def test_pack_hash_deterministic_and_content_sensitive():
    ex = SnippetExtractor()
    a = ex.extract([_chunk("c1", "i-SMR ECCS design.")], query_text="i-SMR ECCS", entities={})
    b = ex.extract([_chunk("c1", "i-SMR ECCS design.")], query_text="i-SMR ECCS", entities={})
    c = ex.extract([_chunk("c1", "Totally different body text.")], query_text="i-SMR ECCS", entities={})
    assert a.pack_hash == b.pack_hash
    assert a.pack_hash != c.pack_hash
    assert a.snippet_extractor_version == "snippet/v1-regex"


def test_empty_body_yields_empty_window():
    ex = SnippetExtractor()
    # body(text or snippet) 가 비면 추출할 문장이 없어 window 는 "".
    pack = ex.extract([_chunk("c1", "")], query_text="q", entities={})
    assert pack.snippets[0].text == ""


def test_tie_break_prefers_earliest_sentence():
    ex = SnippetExtractor(window=0, max_sentences=1)
    # Two sentences with identical score (no entities, same overlap 0) → earliest wins.
    pack = ex.extract(
        [_chunk("c1", "Alpha first. Beta second.")],
        query_text="zzz", entities={},
    )
    assert pack.snippets[0].text == "Alpha first."
