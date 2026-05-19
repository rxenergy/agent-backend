from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.application.tools.errors import ToolUnknown


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    adapter: str
    timeout_ms: int
    retry: int
    required: bool
    endpoint_env: str | None = None


class ToolRegistry:
    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self._specs = specs

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ToolRegistry":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        raw = data.get("tools") or {}
        specs: dict[str, ToolSpec] = {}
        for name, body in raw.items():
            specs[name] = ToolSpec(
                name=name,
                version=str(body.get("version", "v1")),
                adapter=str(body["adapter"]),
                timeout_ms=int(body.get("timeout_ms", 3000)),
                retry=int(body.get("retry", 0)),
                required=bool(body.get("required", False)),
                endpoint_env=body.get("endpoint_env"),
            )
        return cls(specs)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as e:
            raise ToolUnknown(f"Tool not in registry: {name}") from e

    def names(self) -> list[str]:
        return list(self._specs.keys())
