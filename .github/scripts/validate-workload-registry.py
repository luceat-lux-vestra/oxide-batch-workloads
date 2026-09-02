#!/usr/bin/env python3

import json
import stat
import tomllib
from pathlib import Path

SCHEMA_VERSION = 3
CONTRACT_ENTRYPOINT = Path("ci") / "validate"
REQUIRED_ENTRY_KEYS = {"name", "path", "msrv"}


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
        fail("every entry name must be a non-empty string")
    if "/" in name or name in {".", ".."}:
        fail(f"invalid entry name: {name!r}")
    return name


def validate_top_level_path(path_value: object, kind: str) -> str:
    if not isinstance(path_value, str) or not path_value:
        fail(f"every {kind} path must be a non-empty string")
    path = Path(path_value)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        fail(f"{kind} path must be one top-level directory: {path_value!r}")
    return path_value


def read_rust_version(name: str, entry_dir: Path) -> str | None:
    """The single source of truth for MSRV is Cargo.toml's package.rust-version.

    workloads.json never duplicates the version string -- see validate_msrv.
    """
    manifest_path = entry_dir / "Cargo.toml"
    try:
        document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"{name!r} is missing Cargo.toml: {manifest_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"{name!r} has invalid Cargo.toml: {exc}")
    package = document.get("package")
    if not isinstance(package, dict):
        fail(f"{name!r} Cargo.toml is missing a [package] table")
    version = package.get("rust-version")
    if version is None:
        return None
    if not isinstance(version, str) or not version.strip():
        fail(f"{name!r} Cargo.toml package.rust-version must be a non-empty string")
    return version


def validate_msrv(name: str, msrv: object, entry_dir: Path) -> dict:
    if not isinstance(msrv, dict):
        fail(f"{name!r} msrv must be an object")
    declared = msrv.get("declared")
    if not isinstance(declared, bool):
        fail(f"{name!r} msrv.declared must be a boolean")

    rust_version = read_rust_version(name, entry_dir)

    if declared:
        if set(msrv) != {"declared"}:
            fail(
                f"{name!r} declared msrv must contain exactly declared -- the version is always "
                "read from Cargo.toml's package.rust-version, never duplicated in the registry"
            )
        if not rust_version:
            fail(f"{name!r} declares msrv.declared=true but Cargo.toml has no package.rust-version")
        return {"declared": True, "version": rust_version, "policy_reason": None}

    if set(msrv) != {"declared", "policy_reason"}:
        fail(f"{name!r} undeclared msrv must contain exactly declared and policy_reason")
    reason = msrv.get("policy_reason")
    if not isinstance(reason, str) or not reason.strip():
        fail(f"{name!r} msrv.policy_reason must be a non-empty string when msrv is not declared")
    if rust_version:
        fail(
            f"{name!r} msrv.declared is false but Cargo.toml declares "
            f"package.rust-version={rust_version!r}; keep the registry and the manifest consistent"
        )
    return {"declared": False, "version": None, "policy_reason": reason}


def validate_contract_entrypoint(name: str, entry_dir: Path) -> None:
    entrypoint = entry_dir / CONTRACT_ENTRYPOINT
    if not entrypoint.is_file():
        fail(f"{name!r} is missing its CI contract entrypoint: {entrypoint.relative_to(entry_dir.parent)}")
    mode = entrypoint.stat().st_mode
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        fail(f"{name!r} CI contract entrypoint is not executable: {entrypoint.relative_to(entry_dir.parent)}")


def validate_entry(kind: str, entry: object, root: Path, seen_names: set[str], seen_paths: set[str]) -> dict:
    if not isinstance(entry, dict) or set(entry) != REQUIRED_ENTRY_KEYS:
        fail(f"each {kind} entry must contain exactly {', '.join(sorted(REQUIRED_ENTRY_KEYS))}")
    name = validate_name(entry["name"])
    path = validate_top_level_path(entry["path"], kind)
    if name in seen_names:
        fail(f"duplicate name across workloads/fixtures: {name}")
    if path in seen_paths:
        fail(f"duplicate path across workloads/fixtures: {path}")
    seen_names.add(name)
    seen_paths.add(path)

    entry_dir = root / path
    if not entry_dir.is_dir():
        fail(f"registered {kind} path does not exist: {path}")
    if not (entry_dir / "Cargo.toml").is_file():
        fail(f"registered {kind} is missing Cargo.toml: {path}")
    validate_contract_entrypoint(name, entry_dir)
    msrv = validate_msrv(name, entry["msrv"], entry_dir)

    return {"kind": kind, "name": name, "path": path, "msrv": msrv}


def validate_repository(root: Path, registry: Path | None = None) -> dict[str, list[dict]]:
    """Validate workloads.json and return its two structurally separate lists.

    `workloads` are real OxideBatch validation subjects:
    `.github/scripts/validate-oxidebatch-provenance.py` only ever reads this
    key, so there is no field an entry here can set to opt out of #29's
    exact-published-provenance enforcement -- that guarantee is structural,
    not a boolean toggle. `fixtures` are bounded, non-product CI-orchestration
    proofs (see `.github/WORKLOAD_CONTRACT.md`); the provenance validator
    never looks at this key at all.
    """
    registry = registry or root / "workloads.json"
    data = load_registry(registry)
    workloads_raw = data.get("workloads")
    fixtures_raw = data.get("fixtures", [])
    reserved = data.get("reserved_top_level_cargo_projects", [])

    if not isinstance(workloads_raw, list) or not workloads_raw:
        fail("workloads must be a non-empty array")
    if not isinstance(fixtures_raw, list):
        fail("fixtures must be an array")
    if not isinstance(reserved, list):
        fail("reserved_top_level_cargo_projects must be an array")

    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    workloads = [validate_entry("workload", entry, root, seen_names, seen_paths) for entry in workloads_raw]
    fixtures = [validate_entry("fixture", entry, root, seen_names, seen_paths) for entry in fixtures_raw]

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
        if path in seen_paths:
            fail(f"path cannot be both a workload/fixture and a reserved Cargo project: {path}")
        reserved_paths.add(path)
        project_dir = root / path
        if not project_dir.is_dir() or not (project_dir / "Cargo.toml").is_file():
            fail(f"reserved Cargo project must exist and contain Cargo.toml: {path}")

    candidates = {
        child.name
        for child in root.iterdir()
        if child.is_dir() and not child.name.startswith(".") and (child / "Cargo.toml").is_file()
    }
    accounted = seen_paths | reserved_paths

    unregistered = sorted(candidates - accounted)
    stale = sorted(accounted - candidates)
    if unregistered:
        fail("unregistered top-level Cargo project(s): " + ", ".join(unregistered))
    if stale:
        fail("registry references non-candidate Cargo project(s): " + ", ".join(stale))

    return {
        "workloads": sorted(workloads, key=lambda entry: entry["path"]),
        "fixtures": sorted(fixtures, key=lambda entry: entry["path"]),
    }


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        result = validate_repository(root)
    except RegistryError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    workload_names = ", ".join(entry["name"] for entry in result["workloads"])
    fixture_names = ", ".join(entry["name"] for entry in result["fixtures"]) or "(none)"
    print(f"validated {len(result['workloads'])} workload(s): {workload_names}")
    print(f"validated {len(result['fixtures'])} fixture(s): {fixture_names}")


if __name__ == "__main__":
    main()
