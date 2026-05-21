from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.domain.prompting import PromptProfile, compute_composition_hash


@dataclass(frozen=True)
class RenderedPrompt:
    """The single renderable artifact emitted per turn.

    Two reproducibility-related hashes coexist:

      * `composition_hash`  — identity of the fragment combination only
        (independent of context / query). Drives A/B prompt comparison.

      * `rendered_prompt_hash` — full SHA256[:16] of the assembled prompt
        text, the spec §16 reproducibility key.
    """

    profile_id: str
    profile_version: str
    fragment_versions: dict[str, str]
    fragment_hashes: dict[str, str]
    composition_hash: str
    rendered_prompt_hash: str
    text: str
    fragments: dict[str, str]  # relative path → content (audit)
    source: str

    # Convenience alias retained for OTel attribute consumers that already
    # reference the spec field name `rendered_prompt_hash` via `.hash`.
    @property
    def hash(self) -> str:
        return self.rendered_prompt_hash

    @property
    def version(self) -> str:
        return self.profile_version


class PromptRenderer:
    """Assembles a `PromptProfile` + context + query into a `RenderedPrompt`.

    The renderer is pure — fragment I/O happens in the source (LocalPromptSource
    etc.) which has already validated each fragment's sha256.
    """

    def render(
        self,
        profile: PromptProfile,
        *,
        query_text: str,
        context_block: str,
    ) -> RenderedPrompt:
        parts: list[str] = []
        fragments_by_path: dict[str, str] = {}
        fragment_versions: dict[str, str] = {}
        fragment_hashes: dict[str, str] = {}

        for name in ("system", "object", "depth", "cell"):
            frag = profile.fragment(name)
            if frag is None:
                continue
            fragment_versions[name] = frag.version
            fragment_hashes[name] = frag.sha256
            fragments_by_path[frag.path] = frag.content
            header = {
                "system": "# SYSTEM",
                "object": f"# OBJECT [{profile.scenario_object}]",
                "depth": f"# DEPTH [{profile.scenario_depth}]",
                "cell": "# CELL",
            }[name]
            parts.append(f"{header}\n{frag.content}")

        parts.append(f"# CONTEXT\n{context_block}")
        parts.append(f"# QUERY\n{query_text}")
        text = "\n\n".join(parts)
        rendered_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        composition_hash = compute_composition_hash(profile.fragments.values())

        return RenderedPrompt(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            fragment_versions=fragment_versions,
            fragment_hashes=fragment_hashes,
            composition_hash=composition_hash,
            rendered_prompt_hash=rendered_hash,
            text=text,
            fragments=fragments_by_path,
            source=profile.source,
        )

    def to_record(self, rendered: RenderedPrompt, *, query_text: str) -> dict[str, Any]:
        """Sidecar audit record (artifact store payload)."""
        return {
            "prompt_profile_id": rendered.profile_id,
            "prompt_version": rendered.profile_version,
            "prompt_source": rendered.source,
            "composition_hash": rendered.composition_hash,
            "rendered_prompt_hash": rendered.rendered_prompt_hash,
            "fragment_versions": dict(rendered.fragment_versions),
            "fragment_hashes": dict(rendered.fragment_hashes),
            "rendered_prompt": rendered.text,
            "fragments": dict(rendered.fragments),
            "query_text": query_text,
        }
