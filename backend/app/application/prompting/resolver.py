from __future__ import annotations

from app.domain.errors import PromptProfileNotFoundError
from app.domain.prompting import PromptProfile
from app.ports.prompt_source import PromptSourcePort


class PromptResolver:
    """Thin orchestrator over a `PromptSourcePort`.

    Fail-fast: unresolved (O, D) raises `PromptProfileNotFoundError` so the
    runner can emit a first-class refusal instead of silently rendering a
    dummy prompt (spec §6).
    """

    def __init__(self, source: PromptSourcePort) -> None:
        self._source = source

    def resolve(self, scenario_object: str, scenario_depth: str) -> PromptProfile:
        profile = self._source.resolve(scenario_object, scenario_depth)
        if profile is None:
            raise PromptProfileNotFoundError(
                scenario_object=scenario_object, scenario_depth=scenario_depth
            )
        return profile

    def try_resolve(
        self, scenario_object: str, scenario_depth: str
    ) -> PromptProfile | None:
        return self._source.resolve(scenario_object, scenario_depth)

    def all_profiles(self) -> list[PromptProfile]:
        return self._source.all_profiles()

    @property
    def source_id(self) -> str:
        return self._source.source_id
