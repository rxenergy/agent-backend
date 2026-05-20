from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.application.prompting.resolver import (
    FRAGMENT_KEYS,
    FragmentRef,
    PromptProfile,
)


class PromptRegistryError(Exception):
    """Raised when prompts/registry.yaml or its referenced fragments are invalid."""


class LocalPromptSource:
    """Loads prompt profiles from a local Git-tracked directory.

    On construction the registry is eagerly read and each fragment's on-disk
    bytes are SHA256'd against the value declared in `registry.yaml`. Mismatch
    raises `PromptRegistryError` — silently editing a fragment without bumping
    the registry is rejected at boot (spec §4.5 invariant).
    """

    source_id = "local"

    def __init__(self, prompt_dir: str | Path, *, label: str = "mvp") -> None:
        self._dir = Path(prompt_dir)
        self._label = label
        self._by_id: dict[str, PromptProfile] = {}
        self._by_scenario: dict[tuple[str, str], str] = {}
        self._load()

    def _load(self) -> None:
        registry_file = self._dir / "registry.yaml"
        if not registry_file.exists():
            raise PromptRegistryError(
                f"registry.yaml not found under {self._dir}"
            )
        data = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
        profiles = data.get("prompt_profiles") or {}
        if not profiles:
            raise PromptRegistryError("registry.yaml has no prompt_profiles")

        for profile_id, body in profiles.items():
            profile = self._build_profile(profile_id, body)
            self._by_id[profile_id] = profile
            key = (profile.scenario_object, profile.scenario_depth)
            if key in self._by_scenario:
                raise PromptRegistryError(
                    f"duplicate scenario mapping for {key}: "
                    f"{self._by_scenario[key]!r} and {profile_id!r}"
                )
            self._by_scenario[key] = profile_id

    def _build_profile(self, profile_id: str, body: dict[str, Any]) -> PromptProfile:
        fragments_body = body.get("fragments") or {}
        required = {"system", "object", "depth"}
        missing = required - set(fragments_body.keys())
        if missing:
            raise PromptRegistryError(
                f"profile {profile_id!r} missing required fragments: {sorted(missing)}"
            )

        fragments: dict[str, FragmentRef] = {}
        for name in FRAGMENT_KEYS:
            ref_body = fragments_body.get(name)
            if ref_body is None:
                continue  # optional (cell)
            fragments[name] = self._load_fragment(profile_id, name, ref_body)

        schema_body = body.get("output_schema")
        if not schema_body:
            raise PromptRegistryError(
                f"profile {profile_id!r} missing output_schema"
            )
        output_schema = self._load_fragment(profile_id, "output_schema", schema_body)

        return PromptProfile(
            profile_id=profile_id,
            profile_version=str(body.get("profile_version") or body.get("version") or "v1"),
            scenario_object=body["scenario_object"],
            scenario_depth=body["scenario_depth"],
            fragments=fragments,
            output_schema=output_schema,
            model_options=dict(body.get("model_options") or {}),
            source=self.source_id,
        )

    def _load_fragment(
        self, profile_id: str, name: str, ref_body: dict[str, Any]
    ) -> FragmentRef:
        path_rel = ref_body.get("path")
        version = str(ref_body.get("version") or "v1")
        declared_sha = ref_body.get("sha256")
        if not path_rel or not declared_sha:
            raise PromptRegistryError(
                f"profile {profile_id!r} fragment {name!r} requires path + sha256"
            )
        full = self._dir / path_rel
        if not full.exists():
            raise PromptRegistryError(
                f"profile {profile_id!r} fragment {name!r}: file not found at {full}"
            )
        content_bytes = full.read_bytes()
        actual_sha = hashlib.sha256(content_bytes).hexdigest()
        if actual_sha != declared_sha:
            raise PromptRegistryError(
                f"profile {profile_id!r} fragment {name!r} sha mismatch at {path_rel}: "
                f"declared={declared_sha[:12]}…, actual={actual_sha[:12]}… "
                "(bump fragment version + update registry.yaml)"
            )
        return FragmentRef(
            name=name,
            path=path_rel,
            version=version,
            sha256=actual_sha,
            content=content_bytes.decode("utf-8"),
        )

    # ----------------- PromptSourcePort ------------------------------------
    def resolve(self, scenario_object: str, scenario_depth: str) -> PromptProfile | None:
        profile_id = self._by_scenario.get((scenario_object, scenario_depth))
        if profile_id is None:
            return None
        return self._by_id[profile_id]

    def all_profiles(self) -> list[PromptProfile]:
        return list(self._by_id.values())

    @property
    def prompt_dir(self) -> Path:
        return self._dir
