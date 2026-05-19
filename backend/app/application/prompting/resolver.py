from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PromptProfile:
    profile_id: str
    version: str
    scenario_object: str
    scenario_depth: str
    system_path: str
    object_path: str
    depth_path: str
    cell_path: str | None
    output_schema_path: str
    model_options: dict[str, Any]


class PromptResolver:
    """Loads prompt registry from local Git directory.

    Phoenix / hybrid sources are deferred to Phase 3+.
    """

    def __init__(self, prompt_dir: str, label: str = "mvp") -> None:
        self._dir = Path(prompt_dir)
        self._label = label
        self._registry: dict[str, PromptProfile] = {}
        self._by_scenario: dict[tuple[str, str], str] = {}
        self._loaded = False

    def _load(self) -> None:
        registry_file = self._dir / "registry.yaml"
        if not registry_file.exists():
            self._loaded = True
            return
        data = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
        profiles = (data.get("prompt_profiles") or {})
        for profile_id, body in profiles.items():
            profile = PromptProfile(
                profile_id=profile_id,
                version=str(body.get("version", "v1")),
                scenario_object=body["scenario_object"],
                scenario_depth=body["scenario_depth"],
                system_path=body["system"],
                object_path=body["object"],
                depth_path=body["depth"],
                cell_path=body.get("cell") or None,
                output_schema_path=body.get("output_schema", ""),
                model_options=body.get("model_options", {}) or {},
            )
            self._registry[profile_id] = profile
            self._by_scenario[(profile.scenario_object, profile.scenario_depth)] = profile_id
        self._loaded = True

    def resolve(self, scenario_object: str, scenario_depth: str) -> PromptProfile | None:
        if not self._loaded:
            self._load()
        key = (scenario_object, scenario_depth)
        profile_id = self._by_scenario.get(key)
        if profile_id is None:
            return None
        return self._registry[profile_id]

    @property
    def prompt_dir(self) -> Path:
        return self._dir
