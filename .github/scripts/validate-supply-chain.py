#!/usr/bin/env python3
"""Repository-owned supply-chain policy runner (#32).

This is the one place that knows how to turn a *canonical registry name*
into repository-wide Cargo policy plus any workload-owned additional-ecosystem
policy. The GitHub workflow remains a thin registry-driven caller.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
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
ADDITIONAL_SUPPLY_CHAIN_HOOK = Path("ci/validate-supply-chain")
ADVISORY_EXCEPTIONS_FILE = Path(".github/supply-chain-advisory-exceptions.json")
RUSTSEC_ADVISORY_ID = re.compile(r"^RUSTSEC-\d{4}-\d{4}$")
ALLOWED_ADVISORY_EXCEPTION_CLASSIFICATIONS = {"informational/unmaintained"}
ADVISORY_EXCEPTION_FIELDS = {
    "workload",
    "advisory_id",
    "package",
    "classification",
    "owner",
    "reason",
    "remove_when",
}


class SupplyChainError(ValueError):
    pass


def resolve_workload(root: Path, name: str) -> dict:
    """Resolve `name` to its registry entry, restricted to `workloads`."""
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
    """Verify the workload ships a committed Cargo manifest and lockfile."""
    workload_dir = root / entry["path"]
    manifest = workload_dir / "Cargo.toml"
    lockfile = workload_dir / "Cargo.lock"
    if not manifest.is_file():
        raise SupplyChainError(f"missing Cargo.toml for workload {entry['name']!r}: {manifest}")
    if not lockfile.is_file():
        raise SupplyChainError(f"missing Cargo.lock for workload {entry['name']!r}: {lockfile}")
    return manifest


def _nonempty_string(value: object, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupplyChainError(
            f"advisory exception {index} field {field!r} must be a non-empty string"
        )
    return value.strip()


def load_advisory_exceptions(root: Path) -> dict[str, list[dict[str, str]]]:
    """Load and fail-closed validate workload-scoped informational exceptions."""
    path = root / ADVISORY_EXCEPTIONS_FILE
    if not path.exists():
        return {}
    if not path.is_file() or path.is_symlink():
        raise SupplyChainError(f"advisory exception registry must be a regular file: {path}")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupplyChainError(f"invalid advisory exception registry: {exc}") from exc

    if not isinstance(document, dict) or set(document) != {"schema_version", "exceptions"}:
        raise SupplyChainError(
            "advisory exception registry must contain only schema_version and exceptions"
        )
    if document["schema_version"] != 1:
        raise SupplyChainError("advisory exception registry schema_version must be 1")
    if not isinstance(document["exceptions"], list):
        raise SupplyChainError("advisory exception registry exceptions must be an array")

    try:
        registry = validator.validate_repository(root)
    except validator.RegistryError as exc:
        raise SupplyChainError(f"registry validation failed: {exc}") from exc
    workloads = {entry["name"]: entry for entry in registry["workloads"]}

    grouped: dict[str, list[dict[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(document["exceptions"]):
        if not isinstance(raw, dict) or set(raw) != ADVISORY_EXCEPTION_FIELDS:
            raise SupplyChainError(
                f"advisory exception {index} must contain exactly "
                + ", ".join(sorted(ADVISORY_EXCEPTION_FIELDS))
            )

        entry = {
            field: _nonempty_string(raw[field], field, index)
            for field in ADVISORY_EXCEPTION_FIELDS
        }
        workload = entry["workload"]
        advisory_id = entry["advisory_id"]
        package = entry["package"]

        if workload not in workloads:
            raise SupplyChainError(
                f"advisory exception {index} references unknown real workload {workload!r}"
            )
        if not RUSTSEC_ADVISORY_ID.fullmatch(advisory_id):
            raise SupplyChainError(
                f"advisory exception {index} has invalid RustSec ID {advisory_id!r}"
            )
        if entry["classification"] not in ALLOWED_ADVISORY_EXCEPTION_CLASSIFICATIONS:
            raise SupplyChainError(
                f"advisory exception {index} classification must be one of "
                f"{sorted(ALLOWED_ADVISORY_EXCEPTION_CLASSIFICATIONS)!r}"
            )
        if not re.fullmatch(r"#\d+", entry["owner"]):
            raise SupplyChainError(
                f"advisory exception {index} owner must be a repository issue like '#297'"
            )

        key = (workload, advisory_id)
        if key in seen:
            raise SupplyChainError(
                f"duplicate advisory exception for workload {workload!r}: {advisory_id}"
            )
        seen.add(key)

        lockfile = root / workloads[workload]["path"] / "Cargo.lock"
        try:
            lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SupplyChainError(
                f"cannot validate advisory exception package against {lockfile}: {exc}"
            ) from exc
        resolved_packages = {
            item.get("name")
            for item in lock.get("package", [])
            if isinstance(item, dict)
        }
        if package not in resolved_packages:
            raise SupplyChainError(
                f"advisory exception {index} package {package!r} is not resolved by "
                f"workload {workload!r}"
            )

        grouped.setdefault(workload, []).append(entry)

    for entries in grouped.values():
        entries.sort(key=lambda item: item["advisory_id"])
    return grouped


def materialize_effective_deny_config(
    canonical_config: Path,
    exceptions: list[dict[str, str]],
    output: Path,
) -> Path:
    """Render one workload's exact advisory ignores without weakening the base policy."""
    try:
        source = canonical_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SupplyChainError(f"invalid canonical supply-chain policy: {exc}") from exc

    advisories = parsed.get("advisories")
    if not isinstance(advisories, dict):
        raise SupplyChainError("canonical supply-chain policy is missing [advisories]")
    if advisories.get("ignore") != []:
        raise SupplyChainError(
            "canonical deny.toml must remain exception-free; workload exceptions belong "
            f"in {ADVISORY_EXCEPTIONS_FILE}"
        )
    if advisories.get("unused-ignored-advisory") != "deny":
        raise SupplyChainError(
            "canonical deny.toml must keep unused-ignored-advisory = 'deny'"
        )

    advisory_ids = sorted({entry["advisory_id"] for entry in exceptions})
    if not advisory_ids:
        return canonical_config

    pattern = re.compile(r"(?m)^ignore\s*=\s*\[\s*\]\s*$")
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise SupplyChainError(
            "canonical deny.toml must contain exactly one literal empty advisories ignore list"
        )

    rendered_ids = ", ".join(json.dumps(value) for value in advisory_ids)
    rendered = pattern.sub(f"ignore = [{rendered_ids}]", source, count=1)
    try:
        rendered_policy = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise SupplyChainError(f"generated workload policy is invalid TOML: {exc}") from exc
    if rendered_policy["advisories"]["ignore"] != advisory_ids:
        raise SupplyChainError("generated workload advisory ignore list does not match registry")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return output


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


def discover_nested_maven_manifests(workload_dir: Path) -> list[Path]:
    """Return every Maven manifest visible in a fresh workload checkout."""
    return sorted(workload_dir.rglob("pom.xml"))


def resolve_additional_hook(workload_dir: Path) -> Path | None:
    """Fail closed when Maven exists without a workload-owned policy hook.

    The central validator does not learn Java/Maven policy details. It only
    detects the additional ecosystem and requires the stable workload-local
    hook. The hook itself owns exact versions, repository/source restrictions,
    and any ecosystem-specific invariants.
    """
    hook = workload_dir / ADDITIONAL_SUPPLY_CHAIN_HOOK
    maven_manifests = discover_nested_maven_manifests(workload_dir)

    if hook.is_symlink():
        raise SupplyChainError(f"additional supply-chain hook must not be a symlink: {hook}")
    if maven_manifests and not hook.is_file():
        rendered = ", ".join(str(path.relative_to(workload_dir)) for path in maven_manifests)
        raise SupplyChainError(
            "nested Maven manifests require executable ci/validate-supply-chain; found: "
            + rendered
        )
    if hook.exists() and not hook.is_file():
        raise SupplyChainError(f"additional supply-chain hook is not a regular file: {hook}")
    if hook.is_file() and not os.access(hook, os.X_OK):
        raise SupplyChainError(f"additional supply-chain hook is not executable: {hook}")
    return hook if hook.is_file() else None


def run_additional_hook(workload_dir: Path, hook: Path | None) -> int:
    if hook is None:
        return 0
    print(f"running additional supply-chain hook: {hook.relative_to(workload_dir)}")
    completed = subprocess.run([str(hook.resolve())], cwd=workload_dir)
    if completed.returncode == 0:
        print("additional-ecosystem ok")
    else:
        print("additional-ecosystem FAILED")
    return completed.returncode


def run_supply_chain_scan(
    root: Path,
    name: str,
    *,
    deny_config: Path | None = None,
    cargo_deny_bin: str = DEFAULT_CARGO_DENY_BIN,
) -> int:
    """Run Cargo policy and any required nested-ecosystem hook for one workload."""
    entry = resolve_workload(root, name)
    workload_dir = root / entry["path"]
    manifest = verify_locked_graph(root, entry)
    additional_hook = resolve_additional_hook(workload_dir)

    config = deny_config if deny_config is not None else root / "deny.toml"
    if not config.is_file():
        raise SupplyChainError(f"missing canonical supply-chain policy: {config}")

    exceptions = load_advisory_exceptions(root).get(name, [])
    if exceptions:
        rendered = ", ".join(entry["advisory_id"] for entry in exceptions)
        print(f"workload-scoped advisory exceptions for {name}: {rendered}")

    if exceptions:
        with tempfile.TemporaryDirectory(prefix=f"cargo-deny-{name}-") as tempdir:
            effective_config = materialize_effective_deny_config(
                config,
                exceptions,
                Path(tempdir) / "deny.toml",
            )
            command = build_cargo_deny_command(cargo_deny_bin, effective_config, manifest)
            print(f"running: {' '.join(command)}")
            cargo_result = subprocess.run(command)
    else:
        command = build_cargo_deny_command(cargo_deny_bin, config, manifest)
        print(f"running: {' '.join(command)}")
        cargo_result = subprocess.run(command)

    if cargo_result.returncode != 0:
        return cargo_result.returncode
    return run_additional_hook(workload_dir, additional_hook)


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
        print(
            f"::error::supply-chain policy check failed for workload {args.workload!r} "
            f"(exit code {exit_code})"
        )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
