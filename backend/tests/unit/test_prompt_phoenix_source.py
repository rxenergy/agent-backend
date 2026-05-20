from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from app.application.prompting.hybrid_source import HybridPromptSource
from app.application.prompting.local_source import LocalPromptSource
from app.application.prompting.phoenix_source import PhoenixPromptSource
from tests.unit._prompts_fixture import build_prompts


class _FakePhoenixClient:
    """In-memory PhoenixPromptClient double — drives the source without any net I/O."""

    def __init__(self, profiles: dict, fragments: dict[tuple[str, str], dict]) -> None:
        self._profiles = profiles
        self._fragments = fragments

    def list_profiles(self, *, label: str) -> list[dict]:
        return list(self._profiles.values())

    def get_fragment(self, *, profile_id: str, name: str, label: str) -> dict:
        return self._fragments[(profile_id, name)]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _make_phoenix_payload():
    sys_body = "PHX_SYS"
    obj_body = "PHX_OBJ"
    dep_body = "PHX_DEP"
    schema_body = "{}"
    profiles = {
        "o1_d2_v1": {
            "profile_id": "o1_d2_v1",
            "profile_version": "v1",
            "scenario_object": "O1",
            "scenario_depth": "D2",
            "fragments": {"system": True, "object": True, "depth": True},
            "model_options": {"temperature": 0.1},
        }
    }
    fragments = {
        ("o1_d2_v1", "system"): {"content": sys_body, "sha256": _sha(sys_body), "version": "v1"},
        ("o1_d2_v1", "object"): {"content": obj_body, "sha256": _sha(obj_body), "version": "v1"},
        ("o1_d2_v1", "depth"): {"content": dep_body, "sha256": _sha(dep_body), "version": "v1"},
        ("o1_d2_v1", "output_schema"): {"content": schema_body, "sha256": _sha(schema_body), "version": "v1"},
    }
    return profiles, fragments


def test_phoenix_source_builds_profile_from_client() -> None:
    profiles, fragments = _make_phoenix_payload()
    src = PhoenixPromptSource(_FakePhoenixClient(profiles, fragments))

    profile = src.resolve("O1", "D2")
    assert profile is not None
    assert profile.profile_id == "o1_d2_v1"
    assert profile.source == "phoenix"
    assert profile.fragment("system").content == "PHX_SYS"


def test_phoenix_source_rejects_sha_drift() -> None:
    profiles, fragments = _make_phoenix_payload()
    # Tamper: declared sha doesn't match the content the client returns.
    fragments[("o1_d2_v1", "system")] = {
        "content": "PHX_SYS",
        "sha256": "0" * 64,
        "version": "v1",
    }
    src = PhoenixPromptSource(_FakePhoenixClient(profiles, fragments))
    with pytest.raises(RuntimeError) as excinfo:
        src.resolve("O1", "D2")
    assert "sha mismatch" in str(excinfo.value)


def test_hybrid_falls_back_to_local_on_primary_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        local = LocalPromptSource(root)

        class _BrokenPrimary:
            source_id = "phoenix"

            def resolve(self, *_args, **_kw):
                raise RuntimeError("phoenix unreachable")

            def all_profiles(self):
                raise RuntimeError("phoenix unreachable")

        hybrid = HybridPromptSource(primary=_BrokenPrimary(), fallback=local)
        profile = hybrid.resolve("O1", "D2")
        assert profile is not None
        assert profile.source == "hybrid:local"


def test_hybrid_prefers_primary_when_available() -> None:
    profiles, fragments = _make_phoenix_payload()
    phoenix = PhoenixPromptSource(_FakePhoenixClient(profiles, fragments))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_prompts(root)
        local = LocalPromptSource(root)
        hybrid = HybridPromptSource(primary=phoenix, fallback=local)

        profile = hybrid.resolve("O1", "D2")
        assert profile is not None
        assert profile.source == "hybrid:phoenix"
        assert profile.fragment("system").content == "PHX_SYS"
