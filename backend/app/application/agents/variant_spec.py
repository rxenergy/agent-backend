from __future__ import annotations

from pathlib import Path

import yaml

from app.domain.agents import VariantSpec


class VariantSpecRegistryError(Exception):
    """Raised when `variants/registry.yaml` is missing, malformed, or
    declares an unknown field that would silently drop on load."""


class VariantSpecRegistry:
    """Loads and serves `VariantSpec` entries from `variants/registry.yaml`.

    Mirrors `ToolRegistry.from_yaml` for consistency — adapter / policy
    metadata is owned by YAML, not by code (ADR-0006). The registry is
    pure data; runner factories live in `VariantRegistry` (ADR-0004,
    Phase 3.2b).
    """

    def __init__(self, specs: dict[str, VariantSpec]) -> None:
        self._specs = specs

    @classmethod
    def from_yaml(cls, path: str | Path) -> "VariantSpecRegistry":
        p = Path(path)
        if not p.exists():
            raise VariantSpecRegistryError(f"variants registry not found: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw = data.get("variants") or {}
        if not raw:
            raise VariantSpecRegistryError(f"{p} has no `variants:` entries")

        specs: dict[str, VariantSpec] = {}
        for name, body in raw.items():
            body = dict(body or {})
            body.setdefault("variant_id", name)
            if body["variant_id"] != name:
                raise VariantSpecRegistryError(
                    f"variant key {name!r} must match variant_id "
                    f"{body['variant_id']!r}"
                )
            for tuple_key in ("compatible_llms", "required_tools",
                              "required_scenarios", "capability_tags"):
                if tuple_key in body and body[tuple_key] is not None:
                    body[tuple_key] = tuple(body[tuple_key])
            specs[name] = VariantSpec(**body)
        return cls(specs)

    def get(self, variant_id: str) -> VariantSpec:
        try:
            return self._specs[variant_id]
        except KeyError as e:
            raise VariantSpecRegistryError(
                f"variant not in registry: {variant_id!r}; "
                f"known={sorted(self._specs)}"
            ) from e

    def all(self) -> dict[str, VariantSpec]:
        return dict(self._specs)

    def names(self) -> list[str]:
        return list(self._specs.keys())
