#!/usr/bin/env python3

import json
import re
import tomllib
from pathlib import Path

CRATES_IO_SOURCES = {
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
}
EXACT_VERSION = re.compile(r"^=[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
DEPENDENCY_TABLES = {"dependencies", "dev-dependencies", "build-dependencies"}


class ProvenanceError(ValueError):
    pass


def fail(message: str) -> None:
    raise ProvenanceError(message)


def is_first_party(package: str) -> bool:
    return package == "oxide-batch" or package.startswith("oxide-batch-")


def iter_dependency_tables(document: dict):
    for table_name in DEPENDENCY_TABLES:
        table = document.get(table_name)
        if isinstance(table, dict):
            yield table_name, table
    target = document.get("target")
    if isinstance(target, dict):
        for target_name, target_config in target.items():
            if not isinstance(target_config, dict):
                continue
            for table_name in DEPENDENCY_TABLES:
                table = target_config.get(table_name)
                if isinstance(table, dict):
                    yield f"target.{target_name}.{table_name}", table


def dependency_package(alias: str, spec: object) -> tuple[str, str | None, dict | None]:
    if isinstance(spec, str):
        return alias, spec, None
    if not isinstance(spec, dict):
        fail(f"unsupported dependency specification for {alias!r}")
    package = spec.get("package", alias)
    if not isinstance(package, str) or not package:
        fail(f"dependency {alias!r} has invalid package name")
    version = spec.get("version")
    if version is not None and not isinstance(version, str):
        fail(f"dependency {alias!r} has invalid version")
    return package, version, spec


def validate_manifest(manifest_path: Path) -> dict[str, str]:
    try:
        document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing manifest: {manifest_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid manifest {manifest_path}: {exc}")

    patch = document.get("patch")
    if isinstance(patch, dict):
        for registry, entries in patch.items():
            if isinstance(entries, dict):
                for alias, spec in entries.items():
                    package = alias
                    if isinstance(spec, dict) and isinstance(spec.get("package"), str):
                        package = spec["package"]
                    if is_first_party(package):
                        fail(f"first-party dependency {package} must not be overridden by [patch.{registry}]")

    replace = document.get("replace")
    if isinstance(replace, dict):
        for key in replace:
            package = key.split(":", 1)[0]
            if is_first_party(package):
                fail(f"first-party dependency {package} must not be overridden by [replace]")

    subjects: dict[str, str] = {}
    for table_name, table in iter_dependency_tables(document):
        for alias, spec in table.items():
            package, version, detailed = dependency_package(alias, spec)
            if not is_first_party(package):
                continue
            if detailed is not None:
                forbidden = sorted(key for key in ("path", "git", "registry", "workspace") if key in detailed)
                if forbidden:
                    fail(
                        f"first-party dependency {alias!r} ({package}) in [{table_name}] "
                        f"uses forbidden provenance field(s): {', '.join(forbidden)}"
                    )
            if version is None:
                fail(f"first-party dependency {alias!r} ({package}) must declare an explicit exact version")
            if not EXACT_VERSION.fullmatch(version):
                fail(
                    f"first-party dependency {alias!r} ({package}) in [{table_name}] "
                    f"must use an exact =x.y.z version, found {version!r}"
                )
            resolved_version = version[1:]
            previous = subjects.get(package)
            if previous is not None and previous != resolved_version:
                fail(f"first-party dependency {package} is declared at conflicting exact versions")
            subjects[package] = resolved_version

    if not subjects:
        fail(f"workload manifest declares no first-party OxideBatch validation subject: {manifest_path}")
    return subjects


def validate_lockfile(lockfile_path: Path, subjects: dict[str, str]) -> None:
    try:
        document = tomllib.loads(lockfile_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing lockfile: {lockfile_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid lockfile {lockfile_path}: {exc}")
    packages = document.get("package")
    if not isinstance(packages, list):
        fail(f"lockfile has no package array: {lockfile_path}")
    by_name_version: dict[tuple[str, str], list[dict]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            by_name_version.setdefault((name, version), []).append(package)
    for name, version in sorted(subjects.items()):
        matches = by_name_version.get((name, version), [])
        if len(matches) != 1:
            fail(f"lockfile must contain exactly one {name} {version} package, found {len(matches)}")
        package = matches[0]
        source = package.get("source")
        checksum = package.get("checksum")
        if source not in CRATES_IO_SOURCES:
            fail(f"lockfile {name} {version} does not resolve from canonical crates.io: {source!r}")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            fail(f"lockfile {name} {version} is missing a valid crates.io checksum")


def validate_cargo_source_config(base_dir: Path) -> None:
    for relative in (Path(".cargo/config.toml"), Path(".cargo/config")):
        path = base_dir / relative
        if not path.exists():
            continue
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            fail(f"invalid Cargo config {path}: {exc}")
        if "source" in document:
            fail(f"Cargo source replacement is forbidden for validation subjects: {path}")


def load_workloads(root: Path) -> list[Path]:
    """Only ever reads the `workloads` key -- never `fixtures`.

    This is what makes the exemption in .github/WORKLOAD_CONTRACT.md
    structural rather than a per-entry flag: there is no field a real
    workload entry could set to weaken this exact-published-provenance
    enforcement, because it is applied uniformly to every entry this
    function returns.
    """
    try:
        registry = json.loads((root / "workloads.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        fail(f"cannot read canonical workload registry: {exc}")
    entries = registry.get("workloads") if isinstance(registry, dict) else None
    if not isinstance(entries, list) or not entries:
        fail("canonical workload registry contains no workloads")
    paths: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            fail("invalid workload registry entry")
        paths.append(root / entry["path"])
    return paths


def load_fixtures(root: Path) -> list[Path]:
    """Reads the `fixtures` key -- disjoint from, and checked differently
    than, `workloads` (see validate_fixture_manifest)."""
    try:
        registry = json.loads((root / "workloads.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        fail(f"cannot read canonical workload registry: {exc}")
    entries = registry.get("fixtures", []) if isinstance(registry, dict) else []
    if not isinstance(entries, list):
        fail("canonical workload registry fixtures must be an array")
    paths: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            fail("invalid fixture registry entry")
        paths.append(root / entry["path"])
    return paths


def validate_fixture_manifest(manifest_path: Path) -> None:
    """The exact opposite requirement from validate_manifest: a fixture must
    declare ZERO first-party OxideBatch dependencies, direct or dev/build,
    in any target. A fixture proves the CI contract is workload-agnostic; if
    it actually consumed OxideBatch it would be a workload and #29's full
    exact-published-provenance enforcement would need to apply to it --
    registering it under `fixtures` instead would then be a live provenance
    bypass, which is exactly what this check forecloses.
    """
    try:
        document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing manifest: {manifest_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid manifest {manifest_path}: {exc}")

    for table_name, table in iter_dependency_tables(document):
        for alias, spec in table.items():
            package, _version, _detailed = dependency_package(alias, spec)
            if is_first_party(package):
                fail(
                    f"fixture manifest declares first-party dependency {package!r} in [{table_name}] "
                    f"({manifest_path}): fixtures must have zero OxideBatch dependencies -- register "
                    "this as a workload under workloads.json's `workloads` array instead if it "
                    "actually validates a published release"
                )


def validate_fixture_lockfile(lockfile_path: Path) -> None:
    """The resolved dependency graph must contain zero first-party OxideBatch
    packages -- this is what actually closes the escape hatch a manifest-only
    check leaves open.

    validate_fixture_manifest only sees the alias/spec text in the fixture's
    own Cargo.toml. Two real Cargo mechanisms can make that text look clean
    while OxideBatch still ends up in the build: workspace dependency
    inheritance (`{ workspace = true }` resolves the real package from
    [workspace.dependencies], never named at the point validate_fixture_manifest
    reads), and a local/path helper crate that itself depends on OxideBatch
    (the fixture's own manifest never mentions OxideBatch at all). Cargo.lock
    is the resolved ground truth regardless of how a package got there, so
    checking it is what actually proves "zero OxideBatch presence" rather
    than merely "no direct manifest mention".
    """
    try:
        document = tomllib.loads(lockfile_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing lockfile: {lockfile_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid lockfile {lockfile_path}: {exc}")
    packages = document.get("package", [])
    if not isinstance(packages, list):
        fail(f"lockfile package entries must be an array: {lockfile_path}")
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        if isinstance(name, str) and is_first_party(name):
            fail(
                f"fixture lockfile resolves first-party package {name!r} ({lockfile_path}): "
                "fixtures must have zero OxideBatch presence anywhere in the resolved dependency "
                "graph, not merely no direct manifest mention -- register this as a workload "
                "instead if it actually validates a published release"
            )


def validate_repository(root: Path) -> dict[str, dict[str, str]]:
    validate_cargo_source_config(root)
    result: dict[str, dict[str, str]] = {}
    for workload_dir in load_workloads(root):
        validate_cargo_source_config(workload_dir)
        subjects = validate_manifest(workload_dir / "Cargo.toml")
        validate_lockfile(workload_dir / "Cargo.lock", subjects)
        result[workload_dir.name] = subjects
    for fixture_dir in load_fixtures(root):
        validate_fixture_manifest(fixture_dir / "Cargo.toml")
        validate_fixture_lockfile(fixture_dir / "Cargo.lock")
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        result = validate_repository(root)
    except ProvenanceError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    for workload, subjects in sorted(result.items()):
        rendered = ", ".join(f"{name}={version}" for name, version in sorted(subjects.items()))
        print(f"{workload}: {rendered} (crates.io, exact, checksummed)")


if __name__ == "__main__":
    main()
