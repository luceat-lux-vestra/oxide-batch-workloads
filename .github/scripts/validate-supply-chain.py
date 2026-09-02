#!/usr/bin/env python3
"""Repository-owned supply-chain policy runner (#32).

This is the one place that knows how to turn a *canonical registry name*
into an actual `cargo-deny` invocation against that workload's own committed,
locked dependency graph and the root `deny.toml` policy. The GitHub workflow
(.github/workflows/ci.yml) is a thin caller of this script, not a second
implementation of these semantics -- and #33's scheduled audit is expected to
call this exact same script rather than reimplementing the scan.

Identity is always resolved from the already fail-closed-validated
`workloads.json` registry (via validate-workload-registry.py), never from an
arbitrary path a caller supplies: callers pass a `--workload` *name*, and
this script rejects any name that is not a registered real workload --
including a name registered only as a `fixture`, which is never treated as
a supply-chain scanning subject. There is no workload-name special case
anywhere below; every workload is handled by the exact same code path.
"""

import argparse
import subprocess
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-workload-registry.py")
sys.path.insert(0, str(MODULE_PATH.parent))
import importlib.util

SPEC = importlib.util.spec_from_file_location("workload_registry_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

DEFAULT_CARGO_DENY_BIN = "cargo-deny"
POLICY_CLASSES = ("advisories", "licenses", "bans", "sources")


class SupplyChainError(ValueError):
    pass


def resolve_workload(root: Path, name: str) -> dict:
    """Resolve `name` to its registry entry, restricted to `workloads`.

    Never accepts or trusts a path from the caller -- only a canonical
    registry name -- and structurally cannot resolve a `fixtures` entry: a
    name that only exists there is rejected with a specific diagnostic
    rather than silently falling through to "not found".
    """
    try:
        result = validator.validate_repository(root)
    except validator.RegistryError as exc:
        raise SupplyChainError(f"registry validation failed: {exc}") from exc

    for entry in result["workloads"]:
        if entry["name"] == name:
            return entry

    fixture_names = {entry["name"] for entry in result["fixtures"]}
    if name in fixture_names:
        raise SupplyChainError(
            f"{name!r} is registered as a fixture, not a real workload -- fixtures are bounded "
            "CI-orchestration proofs, not supply-chain scanning subjects"
        )
    raise SupplyChainError(f"{name!r} is not a registered real workload")


def verify_locked_graph(root: Path, entry: dict) -> Path:
    """Verify the workload ships a committed manifest and lockfile.

    Returns the manifest path; `cargo-deny --locked` itself is what actually
    enforces the lockfile is up to date with the manifest, but a plainly
    missing file should fail with a clear diagnostic rather than whatever
    cargo's own error text produces.
    """
    workload_dir = root / entry["path"]
    manifest = workload_dir / "Cargo.toml"
    lockfile = workload_dir / "Cargo.lock"
    if not manifest.is_file():
        raise SupplyChainError(f"missing Cargo.toml for workload {entry['name']!r}: {manifest}")
    if not lockfile.is_file():
        raise SupplyChainError(f"missing Cargo.lock for workload {entry['name']!r}: {lockfile}")
    return manifest


def build_cargo_deny_command(cargo_deny_bin: str, deny_config: Path, manifest: Path) -> list[str]:
    return [
        cargo_deny_bin,
        "--config",
        str(deny_config),
        "--manifest-path",
        str(manifest),
        "--locked",
        "--all-features",
        "check",
        *POLICY_CLASSES,
    ]


def run_supply_chain_scan(
    root: Path,
    name: str,
    *,
    deny_config: Path | None = None,
    cargo_deny_bin: str = DEFAULT_CARGO_DENY_BIN,
) -> int:
    """Resolve, verify, and scan `name`. Returns cargo-deny's own exit code.

    The caller (main(), and ultimately the GitHub workflow) propagates this
    exit code verbatim as the shard's pass/fail signal -- this function never
    reinterprets a nonzero cargo-deny exit as anything other than failure.
    """
    entry = resolve_workload(root, name)
    manifest = verify_locked_graph(root, entry)

    config = deny_config if deny_config is not None else root / "deny.toml"
    if not config.is_file():
        raise SupplyChainError(f"missing canonical supply-chain policy: {config}")

    command = build_cargo_deny_command(cargo_deny_bin, config, manifest)
    print(f"running: {' '.join(command)}")
    completed = subprocess.run(command)
    return completed.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        required=True,
        help="canonical registry name of a real workload (never a fixture or an arbitrary path)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (defaults to the actual checkout root)",
    )
    parser.add_argument("--deny-config", type=Path, default=None, help="defaults to <root>/deny.toml")
    parser.add_argument("--cargo-deny-bin", default=DEFAULT_CARGO_DENY_BIN)
    args = parser.parse_args()

    try:
        exit_code = run_supply_chain_scan(
            args.root,
            args.workload,
            deny_config=args.deny_config,
            cargo_deny_bin=args.cargo_deny_bin,
        )
    except SupplyChainError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc

    if exit_code == 0:
        print(f"supply-chain policy check passed for workload {args.workload!r}")
    else:
        print(f"::error::supply-chain policy check failed for workload {args.workload!r} (cargo-deny exit code {exit_code})")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
