from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest
import yaml

from app.application.prompting.local_source import (
    LocalPromptSource,
    PromptRegistryError,
)
from tests.unit._prompts_fixture import build_prompts


def test_local_source_loads_and_resolves_profile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        src = LocalPromptSource(root)

        profile = src.resolve("O1", "D2")
        assert profile is not None
        assert profile.profile_id == "o1_d2_v1"
        assert profile.scenario_object == "O1"
        assert profile.fragment("system") is not None
        assert profile.fragment("cell") is not None
        assert profile.source == "local"

        # cell-less profile resolves without a cell fragment.
        no_cell = src.resolve("O4", "D2")
        assert no_cell is not None
        assert no_cell.fragment("cell") is None


def test_local_source_raises_on_sha_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        # Mutate the system fragment after registry is written → declared sha stale.
        (root / "system" / "sys_v1.md").write_text("MUTATED", encoding="utf-8")
        with pytest.raises(PromptRegistryError) as excinfo:
            LocalPromptSource(root)
        assert "sha mismatch" in str(excinfo.value)


def test_local_source_raises_on_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        (root / "system" / "sys_v1.md").unlink()
        with pytest.raises(PromptRegistryError) as excinfo:
            LocalPromptSource(root)
        assert "file not found" in str(excinfo.value)


def test_local_source_resolve_unknown_scenario_returns_none() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        src = LocalPromptSource(root)
        assert src.resolve("O9", "D9") is None


def test_local_source_rejects_duplicate_scenario() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        # Splice a duplicate (O1, D2) profile into the registry.
        reg_file = root / "registry.yaml"
        data = yaml.safe_load(reg_file.read_text())
        data["prompt_profiles"]["dup_profile"] = data["prompt_profiles"]["o1_d2_v1"]
        reg_file.write_text(yaml.safe_dump(data))
        with pytest.raises(PromptRegistryError) as excinfo:
            LocalPromptSource(root)
        assert "duplicate scenario" in str(excinfo.value)


def test_local_source_fragment_sha_matches_disk() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        src = LocalPromptSource(root)
        profile = src.resolve("O1", "D2")
        assert profile is not None
        sys_frag = profile.fragment("system")
        assert sys_frag is not None
        expected = hashlib.sha256(
            (root / sys_frag.path).read_bytes()
        ).hexdigest()
        assert sys_frag.sha256 == expected
