#!/usr/bin/env python3

import json
import stat
from pathlib import Path

SCHEMA_VERSION = 2
CONTRACT_ENTRYPOINT = Path("ci") / "validate"
REQUIRED_WORKLOAD_KEYS = {"name", "path", "msrv", "provenance"}


class RegistryError(ValueError):
    pass


def fail(message: str) -> None:
    raise RegistryError(message)


def load_registry(registry: Path) -> dict:
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail("missing canonical workload registry: workloads.json")
    except json.JSONDecodeError as exc:
        fail(f"invalid workloads.json: {exc}")
    if not isinstance(data, dict):
        fail("workloads.json root must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        fail(f"workloads.json schema_version must be {SCHEMA_VERSION}")
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


def validate_msrv(name: str, msrv: object) -> dict:
    if not isinstance(msrv, dict):
        fail(f"workload {name!r} msrv must be an object")
    declared = msrv.get("declared")
    if not isinstance(declared, bool):
        fail(f"workload {name!r} msrv.declared must be a boolean")
    if declared:
        if set(msrv) != {"declared", "version"}:
            fail(f"workload {name!r} declared msrv must contain exactly declared and version")
        version = msrv.get("version")
        if not isinstance(version, str) or not version.strip():
            fail(f"workload {name!r} msrv.version must be a non-empty string")
    else:
        if set(msrv) != {"declared", "policy_reason"}:
            fail(f"workload {name!r} undeclared msrv must contain exactly declared and policy_reason")
        reason = msrv.get("policy_reason")
        if not isinstance(reason, str) or not reason.strip():
            fail(f"workload {name!r} msrv.policy_reason must be a non-empty string when msrv is not declared")
    return msrv


def validate_provenance(name: str, provenance: object) -> dict:
    if not isinstance(provenance, dict):
        fail(f"workload {name!r} provenance must be an object")
    required = provenance.get("required")
    if not isinstance(required, bool):
        fail(f"workload {name!r} provenance.required must be a boolean")
    if required:
        if set(provenance) != {"required"}:
            fail(f"workload {name!r} provenance-required entry must contain exactly required")
    else:
        if set(provenance) != {"required", "reason"}:
            fail(f"workload {name!r} provenance-exempt entry must contain exactly required and reason")
        reason = provenance.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            fail(f"workload {name!r} provenance.reason must be a non-empty string when provenance is not required")
    return provenance


def validate_contract_entrypoint(name: str, workload_dir: Path) -> None:
    entrypoint = workload_dir / CONTRACT_ENTRYPOINT
    if not entrypoint.is_file():
        fail(f"workload {name!r} is missing its CI contract entrypoint: {entrypoint.relative_to(workload_dir.parent)}")
    mode = entrypoint.stat().st_mode
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        fail(f"workload {name!r} CI contract entrypoint is not executable: {entrypoint.relative_to(workload_dir.parent)}")


def validate_repository(root: Path, registry: Path | None = None) -> list[dict]:
    registry = registry or root / "workloads.json"
    data = load_registry(registry)
    workloads = data.get("workloads")
    reserved = data.get("reserved_top_level_cargo_projects", [])

    if not isinstance(workloads, list) or not workloads:
        fail("workloads must be a non-empty array")
    if not isinstance(reserved, list):
        fail("reserved_top_level_cargo_projects must be an array")

    names: set[str] = set()
    paths: set[str] = set()
    validated: list[dict] = []

    for entry in workloads:
        if not isinstance(entry, dict) or set(entry) != REQUIRED_WORKLOAD_KEYS:
            fail(f"each workload entry must contain exactly {', '.join(sorted(REQUIRED_WORKLOAD_KEYS))}")
        name = validate_name(entry["name"])
        path = validate_top_level_path(entry["path"], "workload")
        if name in names:
            fail(f"duplicate workload name: {name}")
        if path in paths:
            fail(f"duplicate workload path: {path}")
        names.add(name)
        paths.add(path)

        workload_dir = root / path
        if not workload_dir.is_dir():
            fail(f"registered workload path does not exist: {path}")
        if not (workload_dir / "Cargo.toml").is_file():
            fail(f"registered workload is missing Cargo.toml: {path}")
        validate_contract_entrypoint(name, workload_dir)

        msrv = validate_msrv(name, entry["msrv"])
        provenance = validate_provenance(name, entry["provenance"])
        validated.append({"name": name, "path": path, "msrv": msrv, "provenance": provenance})

    reserved_paths: set[str] = set()
    for entry in reserved:
        if not isinstance(entry, dict) or set(entry) != {"path", "reason"}:
            fail("each reserved Cargo project must contain exactly path and reason")
        path = validate_top_level_path(entry["path"], "reserved Cargo project")
        reason = entry["reason"]
        if not isinstance(reason, str) or not reason.strip():
            fail(f"reserved Cargo project must include a non-empty reason: {path}")
        if path in reserved_paths:
            fail(f"duplicate reserved Cargo project: {path}")
        if path in paths:
            fail(f"path cannot be both workload and reserved Cargo project: {path}")
        reserved_paths.add(path)
        project_dir = root / path
        if not project_dir.is_dir() or not (project_dir / "Cargo.toml").is_file():
            fail(f"reserved Cargo project must exist and contain Cargo.toml: {path}")

    candidates = {
        child.name
        for child in root.iterdir()
        if child.is_dir() and not child.name.startswith(".") and (child / "Cargo.toml").is_file()
    }
    accounted = paths | reserved_paths

    unregistered = sorted(candidates - accounted)
    stale = sorted(accounted - candidates)
    if unregistered:
        fail("unregistered top-level Cargo project(s): " + ", ".join(unregistered))
    if stale:
        fail("registry references non-candidate Cargo project(s): " + ", ".join(stale))

    return sorted(validated, key=lambda entry: entry["path"])


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        entries = validate_repository(root)
    except RegistryError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    names = ", ".join(entry["name"] for entry in entries)
    print(f"validated {len(entries)} workload(s): {names}")


if __name__ == "__main__":
    main()
