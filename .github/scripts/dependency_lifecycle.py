"""Exact tracked dependency ownership, runner and managed-tool invariants."""
import datetime as dt
import json
import re
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

ROOT_NAMES = {"Cargo.toml", "Cargo.lock", "pom.xml", "go.mod", "go.sum", "Dockerfile"}
COMPOSE = re.compile(r"(?:docker-compose|compose)(?:\.[^.]+)?\.ya?ml")
RUNNER = re.compile(r"ubuntu-\d{2}\.(?:0[1-9]|1[0-2])-arm")
STRATEGIES = {"DEPENDABOT", "CHANNEL", "UPDATE_PR", "REVIEWED_FREEZE"}


def validate(root, native, read_yaml, fail):
    root = Path(root)
    tracked = set(subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"], text=True
    ).split("\0")) - {""}
    policy = json.loads((root / ".github/dependency-update-policy.json").read_text(encoding="utf-8"))
    entries = policy.get("entries")
    if not isinstance(entries, list) or not entries:
        fail("dependency lifecycle policy has no entries")
    ids, declaration_owner, runner_owner, native_claims = set(), {}, {}, set()
    for entry in entries:
        ident, strategy = entry.get("id"), entry.get("strategy")
        if not ident or ident in ids or strategy not in STRATEGIES:
            fail("invalid/duplicate dependency lifecycle entry")
        ids.add(ident)
        paths = entry.get("source_paths", [])
        if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths) or not set(paths) <= tracked:
            fail("invalid or stale exact source paths: " + ident)
        if not all(entry.get(k) for k in ("owner", "authority", "mechanism", "validation")):
            fail("missing lifecycle authority: " + ident)
        if strategy == "REVIEWED_FREEZE":
            revisit = entry.get("revisit", {})
            if not all(revisit.get(k) for k in ("review_by", "rationale", "trigger", "evidence")):
                fail("incomplete reviewed constraint: " + ident)
            if dt.date.fromisoformat(revisit["review_by"]) <= dt.datetime.now(dt.timezone.utc).date():
                fail("expired reviewed constraint: " + ident)
        provenance = entry.get("provenance", {})
        if strategy == "DEPENDABOT":
            key = (provenance.get("ecosystem"), provenance.get("directory"))
            if key not in native:
                fail("native owner absent: " + ident)
            native_claims.add(key)
        if entry["kind"] == "dependency_root":
            eco, directory = provenance.get("ecosystem"), provenance.get("directory")
            for path in paths:
                if path in declaration_owner:
                    fail("duplicate dependency declaration owner: " + path)
                declaration_owner[path] = (eco, directory, strategy)
        elif entry["kind"] == "runner":
            if strategy not in {"UPDATE_PR", "REVIEWED_FREEZE"}:
                fail("explicit runner needs reviewed lifecycle: " + ident)
            for path in paths:
                if path in runner_owner:
                    fail("duplicate runner owner: " + path)
                runner_owner[path] = provenance["label"]

    if set(native) != native_claims:
        fail("unreconciled native updater roots: " + str(sorted(set(native) - native_claims)))
    declarations = {p for p in tracked if PurePosixPath(p).name in ROOT_NAMES
        or COMPOSE.fullmatch(PurePosixPath(p).name)}
    # A new lock/checksum companion in an already native-managed directory
    # inherits that manifest's proven owner. New primary manifests must be
    # registered explicitly; a new lock never creates an independent owner.
    for path in sorted(declarations - set(declaration_owner)):
        manifest_name = {"Cargo.lock": "Cargo.toml", "go.sum": "go.mod"}.get(PurePosixPath(path).name)
        if manifest_name:
            manifest = str(PurePosixPath(path).with_name(manifest_name))
            inherited = declaration_owner.get(manifest)
            if inherited and inherited[2] == "DEPENDABOT":
                declaration_owner[path] = inherited
    if declarations != set(declaration_owner):
        fail("missing/stale declaration owners: " + str(sorted(declarations ^ set(declaration_owner))))
    for path, (eco, directory, strategy) in declaration_owner.items():
        relative_dir = str(PurePosixPath(path).parent)
        relative_dir = "/" if relative_dir == "." else "/" + relative_dir
        name = PurePosixPath(path).name
        valid_names = {"cargo": {"Cargo.toml", "Cargo.lock"}, "maven": {"pom.xml"},
                       "gomod": {"go.mod", "go.sum"}, "docker": {"Dockerfile"}}
        if not ((eco == "docker-compose" and COMPOSE.fullmatch(name)) or name in valid_names.get(eco, set())):
            fail("declaration ecosystem mismatch: " + path)
        if name in {"Cargo.lock", "go.sum"}:
            manifest = str(PurePosixPath(path).with_name({"Cargo.lock": "Cargo.toml", "go.sum": "go.mod"}[name]))
            if declaration_owner.get(manifest) != (eco, directory, strategy):
                fail("orphan or conflicting lock/checksum: " + path)
        if relative_dir == directory:
            continue
        if not relative_dir.startswith(directory + "/"):
            fail("dependency root directory mismatch: " + path)
        if eco == "cargo":
            workspace = tomllib.loads((root / directory.lstrip("/") / "Cargo.toml").read_text(encoding="utf-8")).get("workspace", {})
            if relative_dir.removeprefix(directory + "/") not in workspace.get("members", []):
                fail("Cargo declaration not in owning workspace: " + path)
        elif eco == "maven":
            pom = ET.parse(root / directory.lstrip("/") / "pom.xml").getroot()
            members = {node.text for node in pom.findall("./{*}modules/{*}module")}
            if relative_dir.removeprefix(directory + "/") not in members:
                fail("Maven declaration not in owning reactor: " + path)
        else:
            fail("dependency root directory mismatch: " + path)

    expected_workflows = {p for p in tracked if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))}
    if expected_workflows != set(runner_owner):
        fail("missing/stale workflow runner ownership")
    count = 0
    for path in sorted(expected_workflows):
        workflow = read_yaml(root / path)
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            fail("workflow has no jobs: " + path)
        for job in jobs.values():
            if "runs-on" not in job and "uses" in job:
                continue
            runner = job.get("runs-on")
            if not isinstance(runner, str) or runner != runner_owner[path] or not RUNNER.fullmatch(runner):
                fail("unowned or non-native runner: " + path)
            count += 1
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                if isinstance(uses, str) and uses.startswith("taiki-e/install-action@"):
                    opts = step.get("with", {})
                    if opts.get("checksum") not in (True, "true") or opts.get("fallback") != "none" or not re.fullmatch(r"(cargo-deny|sccache|zizmor)", str(opts.get("tool"))):
                        fail("unverified tool installer contract: " + path)
    print(f"dependency lifecycle ownership: PASS ({len(declarations)} declarations, {len(expected_workflows)} workflows, {count} runner occurrences)")
