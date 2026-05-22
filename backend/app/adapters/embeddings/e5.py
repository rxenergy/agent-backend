from __future__ import annotations

import logging
from typing import Sequence


logger = logging.getLogger(__name__)


class E5Encoder:
    """multilingual-e5-large wrapper.

    e5 family expects 'query: ' / 'passage: ' prefixes and L2-normalized
    embeddings for cosine similarity.

    Heavy ML deps (torch, sentence-transformers) are imported lazily so that
    tests and lightweight boots can import this module without requiring them.
    """

    def __init__(
        self,
        model_id: str,
        device: str = "cpu",
        max_seq_len: int = 512,
    ) -> None:
        import torch  # noqa: F401  (ensures torch is present before SentenceTransformer)
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self.device = device
        self.model = SentenceTransformer(model_id, device=device)
        self.model.max_seq_length = max_seq_len
        self.dim = int(self.model.get_sentence_embedding_dimension())
        logger.info("E5Encoder loaded: %s dim=%d device=%s", model_id, self.dim, device)

    def _encode(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        import torch

        prefixed = [f"{prefix}{t}" for t in texts]
        with torch.inference_mode():
            vecs = self.model.encode(
                prefixed,
                batch_size=min(32, len(prefixed)),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return vecs.astype("float32").tolist()

    def encode_query(self, text: str) -> list[float]:
        return self._encode([text], prefix="query: ")[0]

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(list(texts), prefix="query: ")

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(list(texts), prefix="passage: ")

    def warmup(self) -> None:
        self.encode_query("warmup")
