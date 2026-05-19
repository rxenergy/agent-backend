from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.application.prompting.resolver import PromptProfile


@dataclass(frozen=True)
class RenderedPrompt:
    profile_id: str
    version: str
    text: str
    hash: str  # sha256 first 16 hex chars
    fragments: dict[str, str]  # filename → content; for audit


def _read(base: Path, rel: str) -> str:
    if not rel:
        return ""
    path = base / rel
    if not path.exists():
        return f"[missing fragment: {rel}]"
    return path.read_text(encoding="utf-8")


class PromptRenderer:
    def __init__(self, prompt_dir: Path) -> None:
        self._dir = prompt_dir

    def render(
        self,
        profile: PromptProfile,
        *,
        query_text: str,
        context_block: str,
    ) -> RenderedPrompt:
        system = _read(self._dir, profile.system_path)
        obj = _read(self._dir, profile.object_path)
        depth = _read(self._dir, profile.depth_path)
        cell = _read(self._dir, profile.cell_path) if profile.cell_path else ""

        parts = [
            f"# SYSTEM\n{system}",
            f"# OBJECT [{profile.scenario_object}]\n{obj}",
            f"# DEPTH [{profile.scenario_depth}]\n{depth}",
        ]
        if cell:
            parts.append(f"# CELL\n{cell}")
        parts.extend([
            f"# CONTEXT\n{context_block}",
            f"# QUERY\n{query_text}",
        ])
        text = "\n\n".join(parts)
        rendered_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        fragments = {
            profile.system_path: system,
            profile.object_path: obj,
            profile.depth_path: depth,
        }
        if profile.cell_path:
            fragments[profile.cell_path] = cell
        return RenderedPrompt(
            profile_id=profile.profile_id,
            version=profile.version,
            text=text,
            hash=rendered_hash,
            fragments=fragments,
        )

    def to_record(self, rendered: RenderedPrompt, *, query_text: str) -> dict[str, Any]:
        return {
            "prompt_profile_id": rendered.profile_id,
            "prompt_version": rendered.version,
            "rendered_prompt_hash": rendered.hash,
            "rendered_prompt": rendered.text,
            "fragments": rendered.fragments,
            "query_text": query_text,
        }
