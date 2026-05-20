from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.application.prompting.local_source import LocalPromptSource
from app.application.prompting.renderer import PromptRenderer
from app.application.prompting.resolver import (
    PromptResolver,
    compute_composition_hash,
)
from app.domain.errors import PromptProfileNotFoundError
from tests.unit._prompts_fixture import build_prompts


@pytest.fixture()
def resolver() -> tuple[PromptResolver, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    build_prompts(root)
    # Keep TemporaryDirectory alive by closing in finalizer; for unit-test
    # simplicity we leak the dir — pytest tmp_path cleanup happens at process exit.
    resolver = PromptResolver(LocalPromptSource(root))
    return resolver, root


def test_renderer_output_carries_both_hashes(resolver) -> None:
    res, _ = resolver
    profile = res.resolve("O1", "D2")
    renderer = PromptRenderer()

    rendered = renderer.render(profile, query_text="질문", context_block="CTX")

    # Two distinct identity keys.
    assert rendered.composition_hash != rendered.rendered_prompt_hash
    assert len(rendered.composition_hash) == 16
    assert len(rendered.rendered_prompt_hash) == 16

    # Per-fragment metadata propagates.
    assert set(rendered.fragment_versions) >= {"system", "object", "depth"}
    assert "cell" in rendered.fragment_hashes  # (O1, D2) has a cell fragment
    assert rendered.fragment_hashes["system"] == profile.fragment("system").sha256
    assert rendered.profile_version == "v1"
    assert rendered.source == "local"


def test_composition_hash_invariant_under_context_and_query(resolver) -> None:
    res, _ = resolver
    profile = res.resolve("O1", "D2")
    renderer = PromptRenderer()

    a = renderer.render(profile, query_text="Q1", context_block="C1")
    b = renderer.render(profile, query_text="Q2", context_block="C2")

    assert a.composition_hash == b.composition_hash
    assert a.rendered_prompt_hash != b.rendered_prompt_hash


def test_composition_hash_changes_when_fragment_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root_a = Path(tmp) / "a"
        root_b = Path(tmp) / "b"
        build_prompts(root_a)
        build_prompts(root_b)
        # Pre-modify root_b's object fragment + rewrite its registry sha so the
        # source still loads. Easiest: rebuild prompts with different object text
        # by patching content before sha is computed.
        from tests.unit._prompts_fixture import _sha  # internal helper
        import yaml as _yaml

        new_body = "O1 BODY MUTATED"
        (root_b / "object" / "o1_v1.md").write_text(new_body)
        reg = _yaml.safe_load((root_b / "registry.yaml").read_text())
        reg["prompt_profiles"]["o1_d2_v1"]["fragments"]["object"]["sha256"] = _sha(new_body)
        (root_b / "registry.yaml").write_text(_yaml.safe_dump(reg))

        src_a = LocalPromptSource(root_a)
        src_b = LocalPromptSource(root_b)
        renderer = PromptRenderer()
        ra = renderer.render(src_a.resolve("O1", "D2"), query_text="Q", context_block="C")
        rb = renderer.render(src_b.resolve("O1", "D2"), query_text="Q", context_block="C")

        assert ra.composition_hash != rb.composition_hash
        assert ra.rendered_prompt_hash != rb.rendered_prompt_hash


def test_composition_hash_deterministic_and_order_independent(resolver) -> None:
    res, _ = resolver
    profile = res.resolve("O1", "D2")
    h1 = compute_composition_hash(list(profile.fragments.values()))
    h2 = compute_composition_hash(reversed(list(profile.fragments.values())))
    assert h1 == h2


def test_renderer_to_record_carries_audit_fields(resolver) -> None:
    res, _ = resolver
    profile = res.resolve("O1", "D2")
    renderer = PromptRenderer()
    rendered = renderer.render(profile, query_text="Q", context_block="C")

    rec = renderer.to_record(rendered, query_text="Q")

    assert rec["composition_hash"] == rendered.composition_hash
    assert rec["rendered_prompt_hash"] == rendered.rendered_prompt_hash
    assert rec["fragment_hashes"]["system"]
    assert rec["fragment_versions"]["system"] == "v1"
    assert rec["prompt_source"] == "local"
    assert rec["query_text"] == "Q"


def test_resolver_raises_for_unknown_scenario(resolver) -> None:
    res, _ = resolver
    with pytest.raises(PromptProfileNotFoundError):
        res.resolve("O9", "D9")
