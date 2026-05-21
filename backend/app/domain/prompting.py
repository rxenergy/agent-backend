from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

FRAGMENT_KEYS: tuple[str, ...] = ("system", "object", "depth", "cell")


@dataclass(frozen=True)
class FragmentRef:
    """Single composable prompt fragment.

    `sha256` is the **full 64-char hex** digest of the fragment bytes (not the
    truncated 16-char rendered hash). Sources MUST validate that the loaded
    `content` actually hashes to this value — fragment drift is a domain rule
    violation (spec §4.5, §9).
    """

    name: str
    path: str
    version: str
    sha256: str
    content: str


@dataclass(frozen=True)
class PromptProfile:
    """A fully-resolved prompt profile ready for rendering.

    Unlike v1 where the renderer re-read fragment files, fragments are pre-loaded
    and hash-validated by the source. The renderer is pure (no I/O).
    """

    profile_id: str
    profile_version: str
    scenario_object: str
    scenario_depth: str
    fragments: Mapping[str, FragmentRef]      # key ∈ FRAGMENT_KEYS; `cell` optional
    output_schema: FragmentRef
    model_options: Mapping[str, Any]
    source: str                               # "local" | "phoenix" | "hybrid:..."

    def fragment(self, name: str) -> FragmentRef | None:
        return self.fragments.get(name)


def compute_fragment_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_composition_hash(fragments: Iterable[FragmentRef]) -> str:
    """Canonical SHA256[:16] over the sorted (name, sha256) pairs.

    Independent of context / query / model options. Two profiles with identical
    fragment content yield identical composition_hash — the "prompt identity" key
    used for A/B comparison and drift detection.
    """
    items = sorted((f.name, f.sha256) for f in fragments)
    payload = "\n".join(f"{n}:{h}" for n, h in items).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
