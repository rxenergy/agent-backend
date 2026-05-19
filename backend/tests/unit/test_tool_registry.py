from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from app.application.tools.errors import ToolUnknown
from app.application.tools.registry import ToolRegistry


def test_loads_specs_from_yaml() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "registry.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "tools": {
                        "retriever.search": {
                            "version": "v1",
                            "adapter": "local",
                            "timeout_ms": 5000,
                            "retry": 1,
                            "required": True,
                        }
                    }
                }
            )
        )
        reg = ToolRegistry.from_yaml(path)
        spec = reg.get("retriever.search")
        assert spec.adapter == "local"
        assert spec.required is True
        assert spec.timeout_ms == 5000
        assert spec.retry == 1


def test_unknown_tool_raises() -> None:
    reg = ToolRegistry(specs={})
    with pytest.raises(ToolUnknown):
        reg.get("missing.tool")
