from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.embeddings.e5 import E5Encoder
from app.embeddings.fermi import FermiEncoder


@dataclass
class TranslateResult:
    dsl: dict[str, Any]
    dense_dim: int
    sparse_terms: int
    encode_ms: float


class HybridQueryTranslator:
    """Build OpenSearch 3.x hybrid DSL combining e5 dense kNN and
    fermi rank_features sparse retrieval.
    """

    def __init__(
        self,
        e5: E5Encoder,
        fermi: FermiEncoder,
        dense_field: str = "dense_e5",
        sparse_field: str = "sparse_fermi",
        text_field: str = "text",
    ) -> None:
        self.e5 = e5
        self.fermi = fermi
        self.dense_field = dense_field
        self.sparse_field = sparse_field
        self.text_field = text_field

    def translate(
        self,
        query: str,
        top_k: int = 10,
        k_dense: int = 50,
    ) -> TranslateResult:
        t0 = time.perf_counter()
        dense_vec = self.e5.encode_query(query)
        sparse_terms = self.fermi.encode_query(query)
        encode_ms = (time.perf_counter() - t0) * 1000.0

        rank_feature_clauses = [
            {
                "rank_feature": {
                    "field": f"{self.sparse_field}.{tok}",
                    "linear": {},
                    "boost": weight,
                }
            }
            for tok, weight in sparse_terms.items()
        ]

        sparse_query: dict[str, Any]
        if rank_feature_clauses:
            sparse_query = {"bool": {"should": rank_feature_clauses}}
        else:
            # Fallback: degenerate sparse — match nothing on sparse side.
            sparse_query = {"match_none": {}}

        dsl: dict[str, Any] = {
            "size": top_k,
            "_source": {"excludes": [self.dense_field, self.sparse_field]},
            "query": {
                "hybrid": {
                    "queries": [
                        {"match": {self.text_field: {"query": query}}},
                        {
                            "knn": {
                                self.dense_field: {
                                    "vector": dense_vec,
                                    "k": k_dense,
                                }
                            }
                        },
                        sparse_query,
                    ]
                }
            },
        }

        return TranslateResult(
            dsl=dsl,
            dense_dim=len(dense_vec),
            sparse_terms=len(sparse_terms),
            encode_ms=encode_ms,
        )
