#!/usr/bin/env python3
"""Validate `prompts/registry.yaml` integrity.

Checks:
  1. Registry parses + has at least one profile.
  2. Every fragment file referenced by a profile exists.
  3. Each fragment's on-disk SHA256 matches the value declared in the registry.
  4. Every profile has an `output_schema` that is valid JSON.
  5. No two profiles map to the same (scenario_object, scenario_depth).
  6. Warns about fragment files not referenced by any profile.

Exits non-zero on any check failure. Intended for CI / pre-commit / Makefile.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import yaml

FRAGMENT_KEYS = ("system", "object", "depth", "cell")


def _err(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"[warn]  {msg}", file=sys.stderr)


def validate(prompt_dir: Path) -> int:
    registry_file = prompt_dir / "registry.yaml"
    if not registry_file.exists():
        _err(f"registry.yaml not found at {registry_file}")
        return 1
    try:
        data = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        _err(f"registry.yaml parse error: {exc}")
        return 1

    profiles = data.get("prompt_profiles") or {}
    if not profiles:
        _err("registry.yaml has no prompt_profiles")
        return 1

    errors = 0
    referenced: set[Path] = set()
    scenario_map: dict[tuple[str, str], str] = {}

    for profile_id, body in profiles.items():
        fragments = body.get("fragments") or {}
        missing = {"system", "object", "depth"} - set(fragments)
        if missing:
            _err(f"{profile_id}: missing required fragments {sorted(missing)}")
            errors += 1

        for name in FRAGMENT_KEYS:
            ref = fragments.get(name)
            if ref is None:
                continue
            errors += _check_fragment(profile_id, name, ref, prompt_dir, referenced)

        schema = body.get("output_schema")
        if not schema:
            _err(f"{profile_id}: missing output_schema")
            errors += 1
        else:
            errors += _check_fragment(
                profile_id, "output_schema", schema, prompt_dir, referenced
            )
            schema_path = prompt_dir / schema["path"]
            if schema_path.suffix == ".json" and schema_path.exists():
                try:
                    json.loads(schema_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    _err(f"{profile_id}: output_schema {schema['path']} invalid JSON: {exc}")
                    errors += 1

        so = body.get("scenario_object")
        sd = body.get("scenario_depth")
        if so and sd:
            key = (so, sd)
            if key in scenario_map:
                _err(
                    f"{profile_id}: duplicate scenario ({so}, {sd}) "
                    f"already taken by {scenario_map[key]}"
                )
                errors += 1
            else:
                scenario_map[key] = profile_id

    # Warn about orphan fragment files.
    for sub in ("system", "object", "depth", "cell", "schemas"):
        sub_dir = prompt_dir / sub
        if not sub_dir.exists():
            continue
        for file in sub_dir.iterdir():
            if not file.is_file():
                continue
            if file.resolve() not in {p.resolve() for p in referenced}:
                _warn(f"orphan fragment not referenced by any profile: {file.relative_to(prompt_dir)}")

    if errors:
        _err(f"validation FAILED: {errors} error(s)")
        return 1
    print(f"prompts OK: {len(profiles)} profiles validated under {prompt_dir}")
    return 0


def _check_fragment(
    profile_id: str,
    name: str,
    ref: dict,
    prompt_dir: Path,
    referenced: set[Path],
) -> int:
    path_rel = ref.get("path")
    declared = ref.get("sha256")
    if not path_rel or not declared:
        _err(f"{profile_id}.{name}: requires path + sha256")
        return 1
    full = prompt_dir / path_rel
    if not full.exists():
        _err(f"{profile_id}.{name}: file missing at {full}")
        return 1
    referenced.add(full)
    actual = hashlib.sha256(full.read_bytes()).hexdigest()
    if actual != declared:
        _err(
            f"{profile_id}.{name}: sha mismatch at {path_rel}\n"
            f"  declared: {declared}\n"
            f"  actual:   {actual}\n"
            f"  → bump fragment version + update registry.yaml"
        )
        return 1
    return 0


def main() -> int:
    here = Path(__file__).resolve().parent
    default = here.parent / "prompts"
    prompt_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    return validate(prompt_dir)


if __name__ == "__main__":
    sys.exit(main())
