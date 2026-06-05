from __future__ import annotations

import logging
import re
from typing import Sequence


logger = logging.getLogger(__name__)

_TOKEN_SANITIZE_RE = re.compile(r"[.\s]")


def _sanitize_token(token: str) -> str:
    # OpenSearch rank_features sub-field names cannot contain '.' (dot is
    # reserved for nested field paths).
    return _TOKEN_SANITIZE_RE.sub("_", token)


class FermiEncoder:
    """atomic-canyon/fermi-1024 wrapper producing SPLADE-style sparse vectors.

    Returns a dict mapping (sanitized) token strings to positive float weights,
    suitable for OpenSearch ``rank_features`` mapping.

    Heavy ML deps (torch, transformers) are imported lazily.
    """

    def __init__(
        self,
        model_id: str,
        device: str = "cpu",
        max_seq_len: int = 1024,
        top_n: int = 200,
        weight_threshold: float = 0.0,
    ) -> None:
        import torch
        from transformers import AutoModelForMaskedLM, AutoTokenizer

        self.model_id = model_id
        self.device = device
        self.max_seq_len = max_seq_len
        self.top_n = top_n
        self.weight_threshold = weight_threshold

        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForMaskedLM.from_pretrained(model_id).to(device).eval()
        self.vocab_size = int(self.model.config.vocab_size)
        self._id_to_token: list[str] = [
            _sanitize_token(tok)
            for tok in self.tokenizer.convert_ids_to_tokens(range(self.vocab_size))
        ]
        self._special_ids: set[int] = set(self.tokenizer.all_special_ids)
        self._special_ids_tensor = (
            torch.tensor(sorted(self._special_ids), dtype=torch.long)
            if self._special_ids
            else torch.empty(0, dtype=torch.long)
        )
        logger.info(
            "FermiEncoder loaded: %s vocab=%d device=%s",
            model_id,
            self.vocab_size,
            device,
        )

    def _forward(self, texts: Sequence[str]):
        import torch
        import torch.nn.functional as F

        with torch.inference_mode():
            enc = self.tokenizer(
                list(texts),
                padding=True,
                truncation=True,
                max_length=self.max_seq_len,
                return_tensors="pt",
            ).to(self.device)
            out = self.model(**enc).logits  # (B, T, V)
            activation = torch.log1p(F.relu(out))
            attn = enc["attention_mask"].unsqueeze(-1).to(activation.dtype)
            activation = activation.masked_fill(attn == 0, float("-inf"))
            pooled, _ = activation.max(dim=1)  # (B, V)
            pooled = torch.where(
                torch.isfinite(pooled), pooled, torch.zeros_like(pooled)
            )
            if self._special_ids:
                pooled.index_fill_(1, self._special_ids_tensor.to(pooled.device), 0.0)
            return pooled.clone()

    def _to_term_dict(self, pooled_row) -> dict[str, float]:
        import torch

        if self.top_n and self.top_n < pooled_row.numel():
            top_vals, top_idx = torch.topk(pooled_row, k=self.top_n)
        else:
            top_vals = pooled_row
            top_idx = torch.arange(pooled_row.numel(), device=pooled_row.device)

        vals = top_vals.detach().cpu().tolist()
        ids = top_idx.detach().cpu().tolist()
        terms: dict[str, float] = {}
        for i, w in zip(ids, vals):
            if w <= self.weight_threshold:
                continue
            tok = self._id_to_token[i]
            if not tok:
                continue
            prev = terms.get(tok)
            if prev is None or w > prev:
                terms[tok] = float(w)
        return terms

    def encode_query(self, text: str) -> dict[str, float]:
        pooled = self._forward([text])
        return self._to_term_dict(pooled[0])

    def encode_queries(self, texts: Sequence[str]) -> list[dict[str, float]]:
        if not texts:
            return []
        pooled = self._forward(list(texts))
        return [self._to_term_dict(pooled[i]) for i in range(pooled.size(0))]

    def encode_documents(self, texts: Sequence[str]) -> list[dict[str, float]]:
        # SPLADE 는 query/doc 인코딩이 동일 forward — sparse reranker 가 후보 본문을
        # 배치로 희소 벡터화할 때 쓴다(encode_queries 와 동형 배치 경로).
        return self.encode_queries(texts)

    def warmup(self) -> None:
        self.encode_query("warmup")
