"""Shared helper for tests that need a valid v2 prompt registry on disk.

Writes minimal fragment files and a registry.yaml with correct sha256 values
so `LocalPromptSource(...)` constructs without integrity errors. The default
scenarios cover (O1, D2) and (O4, D2), matching what the existing sequential
runner tests rely on.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_prompts(
    root: Path,
    *,
    scenarios: list[tuple[str, str]] | None = None,
) -> None:
    """Materialize a v2 prompt registry under `root`.

    `scenarios` is a list of (object, depth) pairs. Each gets a profile id
    `{o.lower()}_{d.lower()}_v1` and a shared cell-less composition.
    """
    scenarios = scenarios or [("O1", "D2"), ("O4", "D2")]
    for d in ("system", "object", "depth", "cell", "schemas"):
        (root / d).mkdir(parents=True, exist_ok=True)

    fragments = {
        "system/sys_v1.md": "SYS body",
        "object/o1_v1.md": "O1 body",
        "object/o2_v1.md": "O2 body",
        "object/o3_v1.md": "O3 body",
        "object/o4_v1.md": "O4 body",
        "depth/d1_v1.md": "D1 body",
        "depth/d2_v1.md": "D2 body",
        "depth/d3_v1.md": "D3 body",
        "cell/o1_d2_v1.md": "CELL_O1D2",
        "schemas/answer_v1.json": "{}",
    }
    for rel, content in fragments.items():
        (root / rel).write_text(content, encoding="utf-8")

    def ref(rel: str) -> dict:
        return {"path": rel, "version": "v1", "sha256": _sha(fragments[rel])}

    profiles: dict[str, dict] = {}
    for obj, dep in scenarios:
        oid = obj.lower()
        did = dep.lower()
        obj_path = f"object/{oid}_v1.md"
        depth_path = f"depth/{did}_v1.md"
        cell_path = "cell/o1_d2_v1.md" if (obj, dep) == ("O1", "D2") else None
        body = {
            "profile_version": "v1",
            "scenario_object": obj,
            "scenario_depth": dep,
            "fragments": {
                "system": ref("system/sys_v1.md"),
                "object": ref(obj_path),
                "depth": ref(depth_path),
                "cell": ref(cell_path) if cell_path else None,
            },
            "output_schema": ref("schemas/answer_v1.json"),
            "model_options": {"temperature": 0.1},
        }
        profiles[f"{oid}_{did}_v1"] = body

    (root / "registry.yaml").write_text(
        yaml.safe_dump({"prompt_profiles": profiles}), encoding="utf-8"
    )
