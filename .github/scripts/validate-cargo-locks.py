#!/usr/bin/env python3
"""Validate or refresh Cargo.lock files for canonical workload/fixture subjects.

Cargo is the only authority allowed to decide whether a committed lockfile
matches a manifest plus its all-features dependency graph. This script never
infers lock consistency from Git diffs and never edits Cargo.lock itself.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REGISTRY_VALIDATOR_PATH = Path(__file__).with_name("validate-workload-registry.py")
SPEC = importlib.util.spec_from_file_location(
    "workload_registry_validator", REGISTRY_VALIDATOR_PATH
)
assert SPEC and SPEC.loader
registry_validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry_validator)


class CargoLockError(ValueError):
    pass


def canonical_subjects(root: Path) -> tuple[dict, ...]:
    """Return every registry subject that owns a committed Cargo.lock contract."""
    try:
        result = registry_validator.validate_repository(root)
    except registry_validator.RegistryError as exc:
        raise CargoLockError(f"registry validation failed: {exc}") from exc

    # Reserved top-level Cargo projects are intentionally excluded: by contract
    # they are not validation subjects and may not yet own a committed lockfile.
    return tuple(result["workloads"]) + tuple(result["fixtures"])


def resolve_subject(root: Path, name: str) -> dict:
    matches = [entry for entry in canonical_subjects(root) if entry["name"] == name]
    if len(matches) != 1:
        raise CargoLockError(f"{name!r} is not one canonical workload/fixture subject")
    return matches[0]


def cargo_metadata_command(
    manifest: Path,
    *,
    cargo_bin: str,
    locked: bool,
) -> list[str]:
    command = [
        cargo_bin,
        "metadata",
        "--manifest-path",
        str(manifest),
        "--all-features",
        "--format-version",
        "1",
        "--no-deps",
    ]
    if locked:
        command.insert(2, "--locked")
    return command


def _require_regular(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise CargoLockError(f"{label} must be a regular file: {path}")


def _diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    detail = stderr or stdout or f"cargo exited {completed.returncode}"
    lines = detail.splitlines()
    return "\n".join(lines[-12:])


def check_subject(root: Path, entry: dict, *, cargo_bin: str = "cargo") -> None:
    workload_dir = root / entry["path"]
    manifest = workload_dir / "Cargo.toml"
    lockfile = workload_dir / "Cargo.lock"
    _require_regular(manifest, f"{entry['name']} Cargo.toml")
    _require_regular(lockfile, f"{entry['name']} Cargo.lock")

    command = cargo_metadata_command(manifest, cargo_bin=cargo_bin, locked=True)
    completed = subprocess.run(
        command,
        cwd=workload_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CargoLockError(
            f"{entry['name']}: Cargo.lock is not resolver-consistent with "
            "Cargo.toml under --all-features. Never hand-edit the lockfile; "
            f"run this script's refresh command. Cargo diagnostic:\n{_diagnostic(completed)}"
        )


def check_all(
    root: Path,
    entries: Iterable[dict],
    *,
    cargo_bin: str = "cargo",
) -> None:
    failures: list[str] = []
    for entry in entries:
        try:
            check_subject(root, entry, cargo_bin=cargo_bin)
            print(f"cargo-lock ok: {entry['name']}")
        except CargoLockError as exc:
            failures.append(str(exc))
    if failures:
        raise CargoLockError("\n\n".join(failures))


def refresh_subject(root: Path, entry: dict, *, cargo_bin: str = "cargo") -> None:
    workload_dir = root / entry["path"]
    manifest = workload_dir / "Cargo.toml"
    _require_regular(manifest, f"{entry['name']} Cargo.toml")

    # Deliberately omit --locked: Cargo itself owns the complete lockfile
    # mutation needed by this manifest's all-features graph.
    command = cargo_metadata_command(manifest, cargo_bin=cargo_bin, locked=False)
    completed = subprocess.run(
        command,
        cwd=workload_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CargoLockError(
            f"{entry['name']}: Cargo could not refresh the dependency graph:\n"
            f"{_diagnostic(completed)}"
        )

    # Prove the resulting file is immediately consumable by reproducible
    # --locked callers before reporting success.
    check_subject(root, entry, cargo_bin=cargo_bin)
    print(f"cargo-lock refreshed and verified: {entry['name']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root",
    )
    parser.add_argument(
        "--cargo-bin",
        default="cargo",
        help="Cargo executable (primarily for contract tests)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="check only this canonical workload/fixture; repeatable",
    )

    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument(
        "--name",
        required=True,
        help="canonical workload/fixture whose lockfile Cargo should refresh",
    )

    args = parser.parse_args()
    root = args.root.resolve()

    try:
        if args.command == "check":
            if args.name:
                entries = tuple(resolve_subject(root, name) for name in args.name)
            else:
                entries = canonical_subjects(root)
            check_all(root, entries, cargo_bin=args.cargo_bin)
        else:
            entry = resolve_subject(root, args.name)
            refresh_subject(root, entry, cargo_bin=args.cargo_bin)
    except CargoLockError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
