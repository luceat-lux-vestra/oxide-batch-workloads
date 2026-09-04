#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

SCHEMA_VERSION = 2
MANIFEST_NAME = "evidence-manifest.json"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REGULAR_GIT_MODES = {"100644", "100755"}
TRUST_CLASSES = {
    "recorded-metadata",
    "schema-checked",
    "deterministically-recomputed",
    "trusted-producer-bound",
}
MODULE_DIR = Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    path = MODULE_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load repository validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTRY = load_module("workload_registry_validator_for_evidence", "validate-workload-registry.py")
PROVENANCE = load_module("oxidebatch_provenance_validator_for_evidence", "validate-oxidebatch-provenance.py")


class EvidenceError(ValueError):
    pass


def fail(message: str) -> None:
    raise EvidenceError(message)


def require_keys(value: object, expected: set[str], field: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        fail(f"{field} must contain exactly {', '.join(sorted(expected))}")
    return value


def require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"{field} must be a non-empty string")
    return value


def require_hex(value: object, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail(f"{field} must be lowercase hexadecimal with the required length")
    return value


def safe_path(value: object, field: str) -> str:
    value = require_string(value, field)
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"{field} must be a safe relative path: {value!r}")
    return path.as_posix()


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        fail(f"git {' '.join(args)} failed: {detail}")
    return completed


def commit_available(root: Path, revision: str) -> bool:
    return git(root, "cat-file", "-e", f"{revision}^{{commit}}", check=False).returncode == 0


def git_show(root: Path, revision: str, repo_path: str, required: bool = True) -> bytes | None:
    completed = git(root, "show", f"{revision}:{repo_path}", check=False)
    if completed.returncode != 0:
        if required:
            fail(f"repository revision {revision} is missing required path {repo_path}")
        return None
    return completed.stdout


def git_blob_bytes(root: Path, oid: str) -> bytes:
    kind = git(root, "cat-file", "-t", oid, check=False)
    if kind.returncode != 0 or kind.stdout.strip() != b"blob":
        fail(f"semantic closure blob {oid} is unavailable from repository history")
    return git(root, "cat-file", "blob", oid).stdout


def git_blob_oid(root: Path, revision: str, repo_path: str) -> str:
    line = git(root, "ls-tree", revision, "--", repo_path).stdout.decode().strip()
    if not line:
        fail(f"repository revision {revision} is missing required blob {repo_path}")
    lines = line.splitlines()
    if len(lines) != 1:
        fail(f"expected one git tree entry for {repo_path}, found {len(lines)}")
    meta, returned_path = lines[0].split("\t", 1)
    _mode, kind, oid = meta.split(" ", 2)
    if kind != "blob" or returned_path != repo_path or not HEX40.fullmatch(oid):
        fail(f"unexpected git tree entry for {repo_path}: {lines[0]!r}")
    return oid


def workload_tree(
    root: Path,
    revision: str,
    workload_path: str,
    *,
    required: bool = True,
) -> dict[str, tuple[str, str]]:
    completed = git(root, "ls-tree", "-r", "-z", revision, "--", workload_path, check=False)
    if completed.returncode != 0:
        if required:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            fail(f"cannot read workload tree {workload_path} at {revision}: {detail}")
        return {}
    raw = completed.stdout
    prefix = workload_path.rstrip("/") + "/"
    result: dict[str, tuple[str, str]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            meta, raw_path = entry.split(b"\t", 1)
            mode_b, kind_b, oid_b = meta.split(b" ", 2)
            repo_path = raw_path.decode("utf-8")
            mode = mode_b.decode("ascii")
            kind = kind_b.decode("ascii")
            oid = oid_b.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            fail(f"malformed git ls-tree output for {workload_path}: {exc}")
        if kind != "blob":
            continue
        if not repo_path.startswith(prefix):
            fail(f"git tree escaped workload prefix: {repo_path}")
        result[repo_path[len(prefix):]] = (mode, oid)
    if not result and required:
        fail(f"repository revision {revision} contains no files for workload {workload_path}")
    return result


def try_select_closure(
    tree: dict[str, tuple[str, str]], includes: list[str]
) -> list[tuple[str, str, str]] | None:
    selected: dict[str, tuple[str, str]] = {}
    for selector in includes:
        matched = {
            path: identity
            for path, identity in tree.items()
            if path == selector or path.startswith(selector.rstrip("/") + "/")
        }
        if not matched:
            return None
        selected.update(matched)
    return [(path, mode, oid) for path, (mode, oid) in sorted(selected.items())]


def select_closure(
    tree: dict[str, tuple[str, str]], includes: list[str]
) -> list[tuple[str, str, str]]:
    entries = try_select_closure(tree, includes)
    if entries is None:
        missing = next(
            selector
            for selector in includes
            if not any(
                path == selector or path.startswith(selector.rstrip("/") + "/")
                for path in tree
            )
        )
        fail(f"semantic_closure include selector matches no producer file: {missing}")
    return entries


def closure_digest(entries: list[tuple[str, str, str]]) -> str:
    payload = b"".join(
        path.encode() + b"\0" + mode.encode() + b"\0" + oid.encode() + b"\n"
        for path, mode, oid in sorted(entries)
    )
    return hashlib.sha256(payload).hexdigest()


def validate_producer(value: object, root: Path) -> tuple[str, bool]:
    producer = require_keys(value, {"base_revision", "revision_role", "run"}, "producer")
    revision = require_hex(producer["base_revision"], HEX40, "producer.base_revision")
    if producer["revision_role"] not in {"producer-checkout", "legacy-source-snapshot"}:
        fail("producer.revision_role must be producer-checkout or legacy-source-snapshot")
    run = require_keys(producer["run"], {"kind", "identity", "binding"}, "producer.run")
    require_string(run["kind"], "producer.run.kind")
    if run["identity"] is not None:
        require_string(run["identity"], "producer.run.identity")
    if run["binding"] not in TRUST_CLASSES:
        fail("producer.run.binding is not a supported trust class")
    if run["binding"] == "trusted-producer-bound":
        fail(
            "evidence manifest v2 has no external attestation verifier; do not claim "
            "trusted-producer-bound without an implemented trust anchor"
        )
    return revision, commit_available(root, revision)


def validate_records(workload_dir: Path, value: object) -> tuple[set[str], int, int]:
    if not isinstance(value, list) or not value:
        fail("records must be a non-empty array")
    paths: set[str] = set()
    scenarios: set[str] = set()
    total_bytes = 0
    for index, record in enumerate(value):
        field = f"records[{index}]"
        record = require_keys(record, {"scenario", "artifact", "input", "parameters", "failure_point"}, field)
        scenario = require_string(record["scenario"], f"{field}.scenario")
        if scenario in scenarios:
            fail(f"duplicate scenario in evidence records: {scenario}")
        scenarios.add(scenario)

        artifact = require_keys(record["artifact"], {"path", "sha256", "size_bytes"}, f"{field}.artifact")
        artifact_path = safe_path(artifact["path"], f"{field}.artifact.path")
        if not artifact_path.startswith("validation/") or Path(artifact_path).name == MANIFEST_NAME:
            fail(f"{field}.artifact.path must be a generated file under validation/")
        if artifact_path in paths:
            fail(f"duplicate retained artifact path: {artifact_path}")
        paths.add(artifact_path)
        expected_sha = require_hex(artifact["sha256"], HEX64, f"{field}.artifact.sha256")
        expected_size = artifact["size_bytes"]
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
            fail(f"{field}.artifact.size_bytes must be a non-negative integer")
        current = workload_dir / artifact_path
        if not current.is_file():
            fail(f"missing retained evidence artifact: {artifact_path}")
        raw = current.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            fail(f"retained evidence artifact digest mismatch for {artifact_path}")
        if len(raw) != expected_size:
            fail(f"retained evidence artifact size mismatch for {artifact_path}")
        total_bytes += len(raw)

        input_value = require_keys(record["input"], {"identity", "reproduction"}, f"{field}.input")
        identity = input_value["identity"]
        if not isinstance(identity, dict):
            fail(f"{field}.input.identity must be an object")
        require_string(identity.get("kind"), f"{field}.input.identity.kind")
        if "sha256" not in identity and "reference" not in identity:
            fail(f"{field}.input.identity must provide sha256 and/or reference")
        if "sha256" in identity:
            require_hex(identity["sha256"], HEX64, f"{field}.input.identity.sha256")
        if "reference" in identity:
            require_string(identity["reference"], f"{field}.input.identity.reference")
        if "size_bytes" in identity:
            size = identity["size_bytes"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                fail(f"{field}.input.identity.size_bytes must be a non-negative integer")
        if not isinstance(input_value["reproduction"], dict) or not input_value["reproduction"]:
            fail(f"{field}.input.reproduction must be a non-empty object")
        if not isinstance(record["parameters"], dict):
            fail(f"{field}.parameters must be an object")
        if record["failure_point"] is not None and (
            not isinstance(record["failure_point"], dict) or not record["failure_point"]
        ):
            fail(f"{field}.failure_point must be null or a non-empty object")
    return paths, len(value), total_bytes


def parse_closure_entries(value: object, includes: list[str]) -> list[tuple[str, str, str]]:
    if not isinstance(value, list) or not value:
        fail("semantic_closure.entries must be a non-empty array")
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        field = f"semantic_closure.entries[{index}]"
        item = require_keys(item, {"path", "mode", "git_blob_oid"}, field)
        path = safe_path(item["path"], f"{field}.path")
        if path in seen:
            fail(f"semantic_closure.entries contains duplicate path: {path}")
        seen.add(path)
        mode = require_string(item["mode"], f"{field}.mode")
        if mode not in REGULAR_GIT_MODES:
            fail(f"{field}.mode must be a regular Git blob mode (100644 or 100755)")
        oid = require_hex(item["git_blob_oid"], HEX40, f"{field}.git_blob_oid")
        if not any(
            path == selector or path.startswith(selector.rstrip("/") + "/")
            for selector in includes
        ):
            fail(f"{field}.path is outside semantic_closure.includes: {path}")
        entries.append((path, mode, oid))
    if [path for path, _mode, _oid in entries] != sorted(seen):
        fail("semantic_closure.entries must be sorted by path")
    for selector in includes:
        if not any(
            path == selector or path.startswith(selector.rstrip("/") + "/")
            for path, _mode, _oid in entries
        ):
            fail(f"semantic_closure include selector has no recorded entry: {selector}")
    return entries


def find_reachable_closure_representation(
    root: Path,
    workload_path: str,
    includes: list[str],
    expected_entries: list[tuple[str, str, str]],
) -> str:
    revisions = git(root, "rev-list", "--reverse", "HEAD").stdout.decode("ascii", errors="strict").splitlines()
    if not revisions:
        fail("repository HEAD has no reachable commit history")
    for revision in revisions:
        tree = workload_tree(root, revision, workload_path, required=False)
        if not tree:
            continue
        entries = try_select_closure(tree, includes)
        if entries == expected_entries:
            return revision
    fail(
        "semantic closure is not represented by any commit reachable from HEAD; "
        "squash/rebase integration must preserve the exact path/mode/blob identities "
        "recorded before retained evidence was committed"
    )


def validate_semantic_closure(
    root: Path,
    workload_path: str,
    producer_revision: str,
    producer_available: bool,
    value: object,
    artifact_paths: set[str],
) -> tuple[dict[str, tuple[str, str]], str]:
    closure = require_keys(
        value,
        {"algorithm", "includes", "excluded_generated_paths", "entries", "digest_sha256"},
        "semantic_closure",
    )
    if closure["algorithm"] != "sha256-git-tree-entries-v1":
        fail("semantic_closure.algorithm must be 'sha256-git-tree-entries-v1'")
    if not isinstance(closure["includes"], list) or not closure["includes"]:
        fail("semantic_closure.includes must be a non-empty array")
    includes = [safe_path(item, "semantic_closure.includes[]") for item in closure["includes"]]
    if len(includes) != len(set(includes)):
        fail("semantic_closure.includes contains duplicates")
    if not isinstance(closure["excluded_generated_paths"], list) or not closure["excluded_generated_paths"]:
        fail("semantic_closure.excluded_generated_paths must be a non-empty array")
    excluded = {
        safe_path(item, "semantic_closure.excluded_generated_paths[]")
        for item in closure["excluded_generated_paths"]
    }
    if len(excluded) != len(closure["excluded_generated_paths"]):
        fail("semantic_closure.excluded_generated_paths contains duplicates")
    if excluded != artifact_paths:
        fail("semantic_closure.excluded_generated_paths must exactly match retained generated artifacts")

    entries = parse_closure_entries(closure["entries"], includes)
    selected = {path for path, _mode, _oid in entries}
    leaked = sorted(selected & excluded)
    if leaked:
        fail("generated evidence must be excluded from semantic closure: " + ", ".join(leaked))
    if f"validation/{MANIFEST_NAME}" in selected:
        fail("evidence manifest must never be included in its own semantic closure")

    expected = require_hex(closure["digest_sha256"], HEX64, "semantic_closure.digest_sha256")
    actual = closure_digest(entries)
    if actual != expected:
        fail(f"semantic closure mismatch: recorded {expected}, recomputed {actual}")

    representation_revision = find_reachable_closure_representation(
        root, workload_path, includes, entries
    )

    if producer_available:
        producer_entries = select_closure(
            workload_tree(root, producer_revision, workload_path), includes
        )
        if producer_entries != entries:
            fail(
                "producer.base_revision semantic closure does not match the recorded "
                "path/mode/blob identities"
            )

    entry_map = {path: (mode, oid) for path, mode, oid in entries}
    return entry_map, representation_revision


def closure_blob_oid(
    entries: dict[str, tuple[str, str]], path: str, field: str
) -> str:
    identity = entries.get(path)
    if identity is None:
        fail(f"{field} must be included in semantic_closure.entries: {path}")
    _mode, oid = identity
    return oid


def historical_provenance(
    root: Path,
    workload_path: str,
    entries: dict[str, tuple[str, str]],
    context_revision: str,
) -> tuple[list[dict[str, str]], str]:
    manifest_oid = closure_blob_oid(entries, "Cargo.toml", "validation_subject")
    lock_oid = closure_blob_oid(entries, "Cargo.lock", "validation_subject")
    manifest_bytes = git_blob_bytes(root, manifest_oid)
    lock_bytes = git_blob_bytes(root, lock_oid)
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        temp_workload = temp_root / workload_path
        temp_workload.mkdir(parents=True)
        (temp_workload / "Cargo.toml").write_bytes(manifest_bytes)
        (temp_workload / "Cargo.lock").write_bytes(lock_bytes)
        for prefix, target in (("", temp_root), (f"{workload_path}/", temp_workload)):
            for relative in (".cargo/config.toml", ".cargo/config"):
                raw = git_show(root, context_revision, prefix + relative, required=False)
                if raw is not None:
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(raw)
        try:
            PROVENANCE.validate_cargo_source_config(temp_root)
            PROVENANCE.validate_cargo_source_config(temp_workload)
            subjects = PROVENANCE.validate_manifest(temp_workload / "Cargo.toml")
            PROVENANCE.validate_lockfile(temp_workload / "Cargo.lock", subjects)
        except PROVENANCE.ProvenanceError as exc:
            fail(f"producer semantic snapshot violates #29 published-provenance contract: {exc}")

    try:
        lock_document = tomllib.loads(lock_bytes.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse producer Cargo.lock: {exc}")
    packages = lock_document.get("package")
    if not isinstance(packages, list):
        fail("producer Cargo.lock has no package array")
    metadata: list[dict[str, str]] = []
    for name, version in sorted(subjects.items()):
        matches = [
            package for package in packages
            if isinstance(package, dict)
            and package.get("name") == name
            and package.get("version") == version
        ]
        if len(matches) != 1:
            fail(f"producer Cargo.lock must contain exactly one {name} {version}")
        package = matches[0]
        metadata.append({
            "name": name,
            "version": version,
            "source": str(package.get("source")),
            "checksum": str(package.get("checksum")),
        })
    return metadata, lock_oid


def validate_subject(
    root: Path,
    workload_path: str,
    entries: dict[str, tuple[str, str]],
    context_revision: str,
    value: object,
) -> None:
    subject = require_keys(value, {"lockfile", "crates"}, "validation_subject")
    lockfile = require_keys(subject["lockfile"], {"path", "git_blob_oid"}, "validation_subject.lockfile")
    if safe_path(lockfile["path"], "validation_subject.lockfile.path") != "Cargo.lock":
        fail("validation_subject.lockfile.path must be Cargo.lock")
    recorded_oid = require_hex(lockfile["git_blob_oid"], HEX40, "validation_subject.lockfile.git_blob_oid")
    expected_crates, actual_oid = historical_provenance(
        root, workload_path, entries, context_revision
    )
    if recorded_oid != actual_oid:
        fail(f"validation subject lockfile identity mismatch: recorded {recorded_oid}, closure has {actual_oid}")
    if not isinstance(subject["crates"], list) or not subject["crates"]:
        fail("validation_subject.crates must be a non-empty array")
    normalized = []
    for index, crate in enumerate(subject["crates"]):
        field = f"validation_subject.crates[{index}]"
        crate = require_keys(crate, {"name", "version", "source", "checksum"}, field)
        normalized.append({
            "name": require_string(crate["name"], f"{field}.name"),
            "version": require_string(crate["version"], f"{field}.version"),
            "source": require_string(crate["source"], f"{field}.source"),
            "checksum": require_hex(crate["checksum"], HEX64, f"{field}.checksum"),
        })
    normalized.sort(key=lambda item: item["name"])
    if normalized != expected_crates:
        fail("validation_subject.crates do not match exact first-party subjects in semantic closure")


def validate_environment(value: object) -> None:
    environment = require_keys(value, {"observations", "limitations"}, "environment")
    if not isinstance(environment["observations"], list) or not environment["observations"]:
        fail("environment.observations must be a non-empty array")
    seen = set()
    for index, observation in enumerate(environment["observations"]):
        field = f"environment.observations[{index}]"
        observation = require_keys(observation, {"name", "value", "trust", "source"}, field)
        name = require_string(observation["name"], f"{field}.name")
        if name in seen:
            fail(f"duplicate environment observation: {name}")
        seen.add(name)
        if observation["value"] is None or isinstance(observation["value"], (dict, list)):
            fail(f"{field}.value must be a scalar")
        if observation["trust"] not in TRUST_CLASSES:
            fail(f"{field}.trust is not a supported trust class")
        require_string(observation["source"], f"{field}.source")
    if not isinstance(environment["limitations"], list) or any(
        not isinstance(item, str) or not item.strip() for item in environment["limitations"]
    ):
        fail("environment.limitations must be an array of non-empty strings")


def validate_retention(workload_dir: Path, value: object, paths: set[str], count: int, total_bytes: int) -> None:
    retention = require_keys(value, {"committed_artifacts", "wall_clock_freshness_merge_gate"}, "retention")
    committed = require_keys(
        retention["committed_artifacts"],
        {"directory", "max_count", "max_total_bytes", "supersession"},
        "retention.committed_artifacts",
    )
    if committed["directory"] != "validation":
        fail("retention.committed_artifacts.directory must be validation")
    max_count = committed["max_count"]
    max_total = committed["max_total_bytes"]
    if not isinstance(max_count, int) or isinstance(max_count, bool) or max_count < 1:
        fail("retention.committed_artifacts.max_count must be a positive integer")
    if not isinstance(max_total, int) or isinstance(max_total, bool) or max_total < 1:
        fail("retention.committed_artifacts.max_total_bytes must be a positive integer")
    require_string(committed["supersession"], "retention.committed_artifacts.supersession")
    if count > max_count:
        fail(f"deterministic retention violation: {count} retained artifacts exceed max_count={max_count}")
    if total_bytes > max_total:
        fail(f"deterministic retention violation: {total_bytes} bytes exceed max_total_bytes={max_total}")
    if retention["wall_clock_freshness_merge_gate"] is not False:
        fail("retention.wall_clock_freshness_merge_gate must be false")
    committed_json = {
        path.relative_to(workload_dir).as_posix()
        for path in (workload_dir / "validation").glob("*.json")
        if path.name != MANIFEST_NAME
    }
    if committed_json != paths:
        fail("deterministic retention/layout violation: retained JSON must exactly match manifest artifacts")


def validate_external_artifacts(value: object) -> None:
    if not isinstance(value, list):
        fail("external_artifacts must be an array")
    for index, artifact in enumerate(value):
        field = f"external_artifacts[{index}]"
        artifact = require_keys(artifact, {"name", "sha256", "reference", "storage", "retention_guarantee"}, field)
        require_string(artifact["name"], f"{field}.name")
        require_hex(artifact["sha256"], HEX64, f"{field}.sha256")
        require_string(artifact["reference"], f"{field}.reference")
        require_string(artifact["storage"], f"{field}.storage")
        require_string(artifact["retention_guarantee"], f"{field}.retention_guarantee")


def validate_verifier(
    workload_dir: Path,
    entries: dict[str, tuple[str, str]],
    value: object,
    manifest_path: Path,
) -> None:
    verifier = require_keys(value, {"canonical", "producer"}, "verifier")
    canonical = require_keys(verifier["canonical"], {"path", "sha256", "result_model"}, "verifier.canonical")
    canonical_path_value = safe_path(canonical["path"], "verifier.canonical.path")
    if canonical["result_model"] != "violations-v1":
        fail("verifier.canonical.result_model must be violations-v1")
    expected_sha = require_hex(canonical["sha256"], HEX64, "verifier.canonical.sha256")
    canonical_path = workload_dir / canonical_path_value
    if not canonical_path.is_file():
        fail(f"canonical verifier does not exist: {canonical_path_value}")
    if hashlib.sha256(canonical_path.read_bytes()).hexdigest() != expected_sha:
        fail("canonical verifier identity mismatch")

    producer = require_keys(verifier["producer"], {"path", "git_blob_oid"}, "verifier.producer")
    producer_path = safe_path(producer["path"], "verifier.producer.path")
    recorded_oid = require_hex(producer["git_blob_oid"], HEX40, "verifier.producer.git_blob_oid")
    actual_oid = closure_blob_oid(entries, producer_path, "verifier.producer")
    if recorded_oid != actual_oid:
        fail("producer verifier identity mismatch")

    completed = subprocess.run(
        [sys.executable, str(canonical_path), "--manifest", str(manifest_path)],
        cwd=workload_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        fail(f"canonical verifier returned malformed JSON: {exc}; stderr={completed.stderr.strip()!r}")
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        fail("canonical verifier result must have schema_version=1")
    violations = result.get("violations")
    if not isinstance(violations, list) or any(not isinstance(item, str) or not item for item in violations):
        fail("canonical verifier result.violations must be non-empty strings")
    if violations:
        fail("canonical verifier reported violation(s): " + "; ".join(violations))
    if completed.returncode != 0:
        fail(f"canonical verifier returned nonzero with no violations: {completed.returncode}")


def validate_manifest(root: Path, workload: dict, manifest_path: Path) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"{workload['name']!r} retains validation evidence but is missing {manifest_path.relative_to(root)}")
    except json.JSONDecodeError as exc:
        fail(f"invalid evidence manifest {manifest_path.relative_to(root)}: {exc}")
    manifest = require_keys(
        manifest,
        {
            "schema_version", "workload", "producer", "semantic_closure",
            "validation_subject", "records", "verifier", "environment",
            "retention", "external_artifacts",
        },
        "evidence manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        fail(f"evidence manifest schema_version must be {SCHEMA_VERSION}")
    if manifest["workload"] != workload["name"]:
        fail(f"evidence manifest workload must be {workload['name']!r}")
    workload_dir = root / workload["path"]
    producer_revision, producer_available = validate_producer(manifest["producer"], root)
    paths, count, total_bytes = validate_records(workload_dir, manifest["records"])
    entries, representation_revision = validate_semantic_closure(
        root,
        workload["path"],
        producer_revision,
        producer_available,
        manifest["semantic_closure"],
        paths,
    )
    context_revision = producer_revision if producer_available else representation_revision
    validate_subject(
        root, workload["path"], entries, context_revision, manifest["validation_subject"]
    )
    validate_environment(manifest["environment"])
    validate_retention(workload_dir, manifest["retention"], paths, count, total_bytes)
    validate_external_artifacts(manifest["external_artifacts"])
    validate_verifier(workload_dir, entries, manifest["verifier"], manifest_path)


def validate_repository(root: Path) -> list[str]:
    try:
        registry = REGISTRY.validate_repository(root)
    except REGISTRY.RegistryError as exc:
        fail(f"canonical workload registry is invalid: {exc}")
    validated = []
    for workload in registry["workloads"]:
        validation_dir = root / workload["path"] / "validation"
        if not validation_dir.exists():
            continue
        if not validation_dir.is_dir():
            fail(f"{workload['name']!r} validation path is not a directory")
        validate_manifest(root, workload, validation_dir / MANIFEST_NAME)
        validated.append(workload["name"])
    return validated


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    try:
        validated = validate_repository(root)
    except EvidenceError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc
    print(
        "validated canonical evidence manifest(s): " + ", ".join(validated)
        if validated
        else "validated canonical evidence contract: no registered workload retains evidence"
    )


if __name__ == "__main__":
    main()
