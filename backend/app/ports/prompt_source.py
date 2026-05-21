from __future__ import annotations

from typing import Protocol

from app.domain.prompting import PromptProfile


class PromptSourcePort(Protocol):
    """Read-side port for prompt registries (local Git / Phoenix / hybrid).

    Located in `ports/` per ADR-0005. Concrete sources live under
    `application/prompting/{local,phoenix,hybrid}_source.py`.
    """

    source_id: str

    def resolve(self, scenario_object: str, scenario_depth: str) -> PromptProfile | None:
        ...

    def all_profiles(self) -> list[PromptProfile]:
        ...
