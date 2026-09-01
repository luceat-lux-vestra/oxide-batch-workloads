#!/usr/bin/env python3

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "workloads.json"


def fail(message: str) -> None:
    print(f"::error::{message}")
    raise SystemExit(1)


def load_registry() -> dict:
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail("missing canonical workload registry: workloads.json")
    except json.JSONDecodeError as exc:
        fail(f"invalid workloads.json: {exc}")
    if not isinstance(data, dict):
        fail("workloads.json root must be an object")
    if data.get("schema_version") != 1:
        fail("workloads.json schema_version must be 1")
    return data


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not name:
        fail("every workload name must be a non-empty string")
    if "/" in name or name in {".", ".."}:
        fail(f"invalid workload name: {name!r}")
    return name


def validate_top_level_path(path_value: object, kind: str) -> str:
    if not isinstance(path_value, str) or not path_value:
        fail(f"every {kind} path must be a non-empty string")
    path = Path(path_value)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        fail(f"{kind} path must be one top-level directory: {path_value!r}")
    return path_value


def main() -> None:
    data = load_registry()
    workloads = data.get("workloads")
    reserved = data.get("reserved_top_level_cargo_projects", [])

    if not isinstance(workloads, list) or not workloads:
        fail("workloads must be a non-empty array")
    if not isinstance(reserved, list):
        fail("reserved_top_level_cargo_projects must be an array")

    names: set[str] = set()
    paths: set[str] = set()

    for entry in workloads:
        if not isinstance(entry, dict) or set(entry) != {"name", "path"}:
            fail("each workload entry must contain exactly name and path")
        name = validate_name(entry["name"])
        path = validate_top_level_path(entry["path"], "workload")
        if name in names:
            fail(f"duplicate workload name: {name}")
        if path in paths:
            fail(f"duplicate workload path: {path}")
        names.add(name)
        paths.add(path)

        workload_dir = ROOT / path
        if not workload_dir.is_dir():
            fail(f"registered workload path does not exist: {path}")
        if not (workload_dir / "Cargo.toml").is_file():
            fail(f"registered workload is missing Cargo.toml: {path}")

    reserved_paths: set[str] = set()
    for entry in reserved:
        path = validate_top_level_path(entry, "reserved Cargo project")
        if path in reserved_paths:
            fail(f"duplicate reserved Cargo project: {path}")
        if path in paths:
            fail(f"path cannot be both workload and reserved Cargo project: {path}")
        reserved_paths.add(path)
        project_dir = ROOT / path
        if not project_dir.is_dir() or not (project_dir / "Cargo.toml").is_file():
            fail(f"reserved Cargo project must exist and contain Cargo.toml: {path}")

    candidates = {
        child.name
        for child in ROOT.iterdir()
        if child.is_dir() and not child.name.startswith(".") and (child / "Cargo.toml").is_file()
    }
    accounted = paths | reserved_paths

    unregistered = sorted(candidates - accounted)
    stale = sorted(accounted - candidates)
    if unregistered:
        fail("unregistered top-level Cargo project(s): " + ", ".join(unregistered))
    if stale:
        fail("registry references non-candidate Cargo project(s): " + ", ".join(stale))

    print(f"validated {len(paths)} workload(s): {', '.join(sorted(paths))}")


if __name__ == "__main__":
    main()
