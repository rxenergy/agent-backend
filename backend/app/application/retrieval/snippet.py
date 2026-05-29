from __future__ import annotations

import hashlib
import re
from typing import Callable

from app.application.retrieval.signals import tokenize
from app.domain.retrieval import EvidencePack, EvidenceSnippet, RetrievedChunk

# v3.1 Node 9 — evidence_snippet (docs/plans/hierarchical_corrective_workflow.v1.md §5).
# raw chunk 전체가 아니라 질의에 가장 관련된 *문장 window* 를 prompt evidence 로
# 추출한다. LLM 미사용·결정론.
#
# 문장 분할: 기본은 결정론 정규식 splitter. spec 의 KSS(한)/spaCy(영)는 무겁고
# (이미지 비대 + onprem air-gapped 정책) v1 코퍼스 검증 전이라 미도입 — splitter 를
# 주입 가능하게 두어 후속 PR 에서 교체한다.

_SENT_RE = re.compile(r"[^.!?。！？\n]+[.!?。！？]?")

SentenceSplitter = Callable[[str], list[str]]


def regex_sentence_split(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.findall(text or "") if s.strip()]


class SnippetExtractor:
    """chunk 본문 → (entity 매칭 + α·query 어휘중첩) argmax 문장 ± window 문장."""

    version = "snippet/v1-regex"

    def __init__(
        self,
        *,
        window: int = 1,
        alpha: float = 0.5,
        max_sentences: int = 3,
        splitter: SentenceSplitter | None = None,
    ) -> None:
        self._window = window
        self._alpha = alpha
        self._max_sentences = max_sentences
        self._split = splitter or regex_sentence_split

    def _chunk_body(self, chunk: RetrievedChunk) -> str:
        return (getattr(chunk, "text", None) or chunk.snippet or "")

    def _best_window(
        self, sentences: list[str], *, query_tokens: set[str], entity_vals: list[str]
    ) -> str:
        if not sentences:
            return ""
        ents = [e.lower() for e in entity_vals if e]

        def score(sent: str) -> float:
            low = sent.lower()
            ent_hits = sum(1 for e in ents if e in low)
            toks = set(tokenize(sent))
            overlap = (len(query_tokens & toks) / len(query_tokens)) if query_tokens else 0.0
            return ent_hits + self._alpha * overlap

        # argmax (동점은 가장 앞 문장 — 결정론).
        best_i = max(range(len(sentences)), key=lambda i: (score(sentences[i]), -i))
        lo = max(0, best_i - self._window)
        hi = min(len(sentences), lo + self._max_sentences)
        return " ".join(sentences[lo:hi]).strip()

    def extract(
        self,
        chunks: list[RetrievedChunk],
        *,
        query_text: str,
        entities: dict[str, list[str]] | None = None,
        citation_ids: list[str] | None = None,
    ) -> EvidencePack:
        query_tokens = set(tokenize(query_text))
        entity_vals = [v for vs in (entities or {}).values() for v in vs if v]
        cids = citation_ids or [f"cite-{i}" for i in range(len(chunks))]

        snippets: list[EvidenceSnippet] = []
        for i, c in enumerate(chunks):
            sentences = self._split(self._chunk_body(c))
            window = self._best_window(
                sentences, query_tokens=query_tokens, entity_vals=entity_vals
            )
            if not window:
                window = (c.snippet or "")[:512]
            snippets.append(
                EvidenceSnippet(
                    snippet_id=f"{c.chunk_id}#s{i}",
                    chunk_id=c.chunk_id,
                    text=window,
                    citation_id=cids[i] if i < len(cids) else None,
                    document_id=c.document_id,
                    section=c.section,
                    page=c.page,
                    revision=c.revision,
                )
            )

        pack_hash = _pack_hash(snippets)
        return EvidencePack(
            snippets=tuple(snippets),
            pack_hash=pack_hash,
            snippet_extractor_version=self.version,
        )


def _pack_hash(snippets: list[EvidenceSnippet]) -> str:
    canon = "\n".join(f"{s.snippet_id}|{s.chunk_id}|{s.text}" for s in snippets)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
