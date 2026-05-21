from __future__ import annotations

from dataclasses import replace

import structlog

from app.domain.prompting import PromptProfile
from app.ports.prompt_source import PromptSourcePort

_log = structlog.get_logger("prompting.hybrid")


class HybridPromptSource:
    """Phoenix primary, Local fallback.

    Any exception raised by the Phoenix path (network, auth, missing profile)
    is logged with structured context and the local source is consulted. The
    returned profile carries `source="hybrid:phoenix"` or `"hybrid:local"` so
    telemetry can distinguish drift between the two registries.
    """

    source_id = "hybrid"

    def __init__(
        self, primary: PromptSourcePort, fallback: PromptSourcePort
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def resolve(self, scenario_object: str, scenario_depth: str) -> PromptProfile | None:
        try:
            profile = self._primary.resolve(scenario_object, scenario_depth)
        except Exception as exc:  # noqa: BLE001 - primary may be remote
            _log.warning(
                "prompt_source_primary_failed",
                scenario_object=scenario_object,
                scenario_depth=scenario_depth,
                primary=self._primary.source_id,
                error=str(exc)[:256],
            )
            profile = None

        if profile is not None:
            return replace(profile, source=f"hybrid:{self._primary.source_id}")

        fb = self._fallback.resolve(scenario_object, scenario_depth)
        if fb is None:
            return None
        _log.info(
            "prompt_source_fallback_used",
            scenario_object=scenario_object,
            scenario_depth=scenario_depth,
            fallback=self._fallback.source_id,
        )
        return replace(fb, source=f"hybrid:{self._fallback.source_id}")

    def all_profiles(self) -> list[PromptProfile]:
        try:
            primary = self._primary.all_profiles()
        except Exception:  # noqa: BLE001
            primary = []
        if primary:
            return [replace(p, source=f"hybrid:{self._primary.source_id}") for p in primary]
        return [
            replace(p, source=f"hybrid:{self._fallback.source_id}")
            for p in self._fallback.all_profiles()
        ]
