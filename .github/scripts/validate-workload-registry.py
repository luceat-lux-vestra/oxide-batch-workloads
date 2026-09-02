#!/usr/bin/env python3

import json
from pathlib import Path


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


def validate_relative_file_path(path_value: object, kind: str) -> str:
    if not isinstance(path_value, str) or not path_value:
        fail(f"{kind} must be a non-empty string path")
    path = Path(path_value)
    if path.is_absolute() or not path.parts:
        fail(f"{kind} must be a relative path: {path_value!r}")
    if any(part in {".", ".."} for part in path.parts):
        fail(f"{kind} must not contain '.' or '..': {path_value!r}")
    return path_value


def validate_contracts(entry: dict, workload_dir: Path, workload_name: str) -> None:
    contracts = entry.get("contracts")
    if not isinstance(contracts, dict) or set(contracts) != {"ci", "msrv"}:
        fail(f"workload {workload_name!r} must define contracts.ci and contracts.msrv")

    ci = contracts.get("ci")
    if not isinstance(ci, dict) or set(ci) != {"run"}:
        fail(f"workload {workload_name!r} contracts.ci must contain exactly run")
    ci_run = validate_relative_file_path(ci.get("run"), f"workload {workload_name!r} contracts.ci.run")
    if not (workload_dir / ci_run).is_file():
        fail(f"workload {workload_name!r} contracts.ci.run path does not exist: {ci_run}")

    msrv = contracts.get("msrv")
    if not isinstance(msrv, dict):
        fail(f"workload {workload_name!r} contracts.msrv must be an object")
    policy = msrv.get("policy")
    if policy == "required":
        if set(msrv) != {"policy", "toolchain", "run"}:
            fail(
                f"workload {workload_name!r} contracts.msrv with policy=required "
                "must contain exactly policy, toolchain, run"
            )
        toolchain = msrv.get("toolchain")
        if not isinstance(toolchain, str) or not toolchain.strip():
            fail(f"workload {workload_name!r} contracts.msrv.toolchain must be a non-empty string")
        msrv_run = validate_relative_file_path(
            msrv.get("run"),
            f"workload {workload_name!r} contracts.msrv.run",
        )
        if not (workload_dir / msrv_run).is_file():
            fail(f"workload {workload_name!r} contracts.msrv.run path does not exist: {msrv_run}")
    elif policy == "not-applicable":
        if set(msrv) != {"policy", "reason"}:
            fail(
                f"workload {workload_name!r} contracts.msrv with policy=not-applicable "
                "must contain exactly policy and reason"
            )
        reason = msrv.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            fail(f"workload {workload_name!r} contracts.msrv.reason must be a non-empty string")
    else:
        fail(f"workload {workload_name!r} contracts.msrv.policy must be required or not-applicable")


def validate_repository(root: Path, registry: Path | None = None) -> list[str]:
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

    for entry in workloads:
        if not isinstance(entry, dict) or set(entry) != {"name", "path", "contracts"}:
            fail("each workload entry must contain exactly name, path, and contracts")
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
        validate_contracts(entry, workload_dir, name)

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

    return sorted(paths)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        paths = validate_repository(root)
    except RegistryError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    print(f"validated {len(paths)} workload(s): {', '.join(paths)}")


if __name__ == "__main__":
    main()
