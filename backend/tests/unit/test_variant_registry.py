from __future__ import annotations

import pytest

import app.application.agents  # noqa: F401 — triggers @register_variant
from app.application.agents.registry import (
    AgentDeps,
    VariantRegistry,
    register_variant,
)
from app.application.agents.variant_spec import VariantSpecRegistry
from app.application.events.recorder import EventRecorder
from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.domain.agents import VariantSpec
from app.domain.interaction import AgentRequest, AgentResponse
from app.ports.agent_runner import AgentRunner


def test_shipped_variants_self_register() -> None:
    """ADR-0004: importing app.application.agents triggers @register_variant
    on every shipped runner module — no edits to profiles.py required."""
    known = VariantRegistry.known()
    assert "spec_driven_v1" in known


def test_yaml_registry_aligns_with_code_registry(tmp_path) -> None:
    """ADR-0006/0004: every spec in variants/registry.yaml must have a code
    factory, and vice versa. Drift is a boot-time error."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[3]
    yaml_path = repo_root / "variants" / "registry.yaml"
    assert yaml_path.exists(), f"missing {yaml_path}"
    yaml_specs = VariantSpecRegistry.from_yaml(yaml_path).names()
    code_specs = VariantRegistry.known()
    assert set(yaml_specs) == set(code_specs), (
        f"yaml={sorted(yaml_specs)} code={sorted(code_specs)}"
    )


def test_register_variant_decorator_adds_to_registry() -> None:
    """A new variant module just needs @register_variant + an __init__ import."""

    class _StubRunner:
        def __init__(self, spec: VariantSpec) -> None:
            self.spec = spec

        async def run(self, request: AgentRequest) -> AgentResponse:  # pragma: no cover
            raise NotImplementedError

    sentinel_id = "plugin_discovery_stub_v0"

    @register_variant(sentinel_id)
    def _factory(spec: VariantSpec, deps: AgentDeps) -> AgentRunner:
        return _StubRunner(spec)  # type: ignore[return-value]

    try:
        assert sentinel_id in VariantRegistry.known()
        stub_spec = VariantSpec(variant_id=sentinel_id)
        # AgentDeps requires non-None recorder/event_sink/app_profile.
        sink = FilesystemEventSink(root="/tmp/_test_events", prefix="t")
        deps = AgentDeps(
            recorder=EventRecorder(sink, app_profile="local"),
            event_sink=sink,
            app_profile="local",
        )
        built = VariantRegistry.build(sentinel_id, stub_spec, deps)
        assert built.spec.variant_id == sentinel_id
    finally:
        # cleanup so other tests don't see this stub
        VariantRegistry._factories.pop(sentinel_id, None)


def test_duplicate_registration_raises() -> None:
    def _f(spec, deps):
        return None  # type: ignore[return-value]

    def _g(spec, deps):
        return None  # type: ignore[return-value]

    VariantRegistry.register("dup_test_v0", _f)
    try:
        with pytest.raises(ValueError, match="duplicate variant_id"):
            VariantRegistry.register("dup_test_v0", _g)
    finally:
        VariantRegistry._factories.pop("dup_test_v0", None)
