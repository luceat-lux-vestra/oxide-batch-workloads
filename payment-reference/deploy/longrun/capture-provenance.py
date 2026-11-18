#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
HOST_SCHEMA = "oxide-batch-workloads.payment-reference-host-provenance"
RUNTIME_SCHEMA = "oxide-batch-workloads.payment-reference-runtime-provenance"
IMAGES_SCHEMA = "oxide-batch-workloads.payment-reference-container-image-provenance"
TOOLCHAINS_SCHEMA = "oxide-batch-workloads.payment-reference-toolchain-provenance"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class QualificationError(RuntimeError):
    pass


class ProvenanceConflict(QualificationError):
    pass


Runner = Callable[[list[str], Path | None], str]


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise QualificationError(f"unable to hash {path}: {exc}") from exc


def run_command(command: list[str], cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise QualificationError(f"unable to execute {command[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise QualificationError(
            f"command failed ({completed.returncode}): {' '.join(command)}"
            + (f": {detail}" if detail else "")
        )
    return (completed.stdout or completed.stderr).strip()


def optional_version(command: list[str], cwd: Path, runner: Runner) -> str | None:
    try:
        value = runner(command, cwd)
    except QualificationError:
        return None
    return value.strip() or None


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(f"unable to read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"{path} must contain a JSON object")
    return value


def load_contract(path: Path) -> dict[str, Any]:
    contract = load_json(path)
    required = {
        "schema",
        "schema_version",
        "campaign_contract",
        "campaign_issue",
        "host_profile",
        "allowed_guest_architectures",
        "minimum_vcpu",
        "minimum_memory_bytes",
        "minimum_disk_total_bytes",
        "minimum_disk_free_bytes",
        "campaign_admission",
        "disk_guard",
        "qualification",
        "required_provenance_files",
    }
    if set(contract) != required:
        raise QualificationError("host qualification contract keys do not match v2")
    if contract["schema_version"] != 2:
        raise QualificationError("host qualification contract schema_version must be 2")
    return contract


def parse_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QualificationError(f"unable to read {path}: {exc}") from exc
    for raw in lines:
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    if not values.get("ID"):
        raise QualificationError(f"{path} does not define ID")
    return dict(sorted(values.items()))


def read_meminfo(path: Path = Path("/proc/meminfo")) -> tuple[int, int]:
    fields: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QualificationError(f"unable to read {path}: {exc}") from exc
    for line in lines:
        name, _, rest = line.partition(":")
        if name not in {"MemTotal", "SwapTotal"}:
            continue
        parts = rest.strip().split()
        if len(parts) != 2 or parts[1] != "kB":
            raise QualificationError(f"unexpected /proc/meminfo format for {name}")
        fields[name] = int(parts[0]) * 1024
    if "MemTotal" not in fields or "SwapTotal" not in fields:
        raise QualificationError("/proc/meminfo missing MemTotal or SwapTotal")
    return fields["MemTotal"], fields["SwapTotal"]


def validate_host_resources(
    contract: dict[str, Any],
    *,
    architecture: str,
    vcpu: int,
    memory_total_bytes: int,
    disk_total_bytes: int,
    disk_free_bytes: int,
    host_sleep_disabled_attested: bool,
) -> None:
    if architecture not in contract["allowed_guest_architectures"]:
        raise QualificationError(
            f"guest architecture {architecture!r} not in {contract['allowed_guest_architectures']!r}"
        )
    if vcpu < contract["minimum_vcpu"]:
        raise QualificationError(f"vCPU {vcpu} < required {contract['minimum_vcpu']}")
    if memory_total_bytes < contract["minimum_memory_bytes"]:
        raise QualificationError(
            f"memory {memory_total_bytes} < required {contract['minimum_memory_bytes']}"
        )
    if disk_total_bytes < contract["minimum_disk_total_bytes"]:
        raise QualificationError(
            f"disk total {disk_total_bytes} < required {contract['minimum_disk_total_bytes']}"
        )
    if disk_free_bytes < contract["minimum_disk_free_bytes"]:
        raise QualificationError(
            f"disk free {disk_free_bytes} < required {contract['minimum_disk_free_bytes']}"
        )
    if not host_sleep_disabled_attested:
        raise QualificationError("host sleep-disabled attestation is required")


def parse_compose_ps(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        raise QualificationError("docker compose ps returned no running services")
    try:
        parsed = json.loads(text)
        values = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        values = []
        for line in text.splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise QualificationError("unable to parse docker compose ps JSON") from exc

    normalized: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            raise QualificationError("docker compose ps entry is not an object")
        service = item.get("Service")
        image_ref = item.get("Image")
        container_name = item.get("Name") or item.get("Names") or ""
        container_id = item.get("ID") or item.get("Id") or ""
        state = item.get("State") or ""
        health = item.get("Health") or ""
        if not isinstance(service, str) or not service:
            raise QualificationError(f"compose ps entry missing Service: {item!r}")
        if not isinstance(image_ref, str) or not image_ref:
            raise QualificationError(f"compose ps entry missing Image: {item!r}")
        if not isinstance(container_name, str) or not container_name:
            raise QualificationError(f"compose ps entry missing Name: {item!r}")
        if not isinstance(container_id, str) or not container_id:
            raise QualificationError(f"compose ps entry missing container ID: {item!r}")
        if state.lower() != "running":
            raise QualificationError(f"compose service {service!r} is not running: state={state!r}")
        if str(health).lower() == "unhealthy":
            raise QualificationError(f"compose service {service!r} is unhealthy")
        normalized.append(
            {
                "service": service,
                "container_name": container_name,
                "container_id": container_id,
                "image_ref": image_ref,
                "state": state,
                "health": str(health),
            }
        )
    normalized.sort(key=lambda item: (item["service"], item["container_name"], item["image_ref"]))
    return normalized


def inspect_images(
    compose_services: list[dict[str, Any]],
    expected_services: list[str],
    repo_root: Path,
    runner: Runner,
) -> list[dict[str, Any]]:
    observed_services = sorted(item["service"] for item in compose_services)
    if observed_services != sorted(expected_services):
        raise QualificationError(
            f"running Compose services disagree with config: expected={sorted(expected_services)!r} "
            f"observed={observed_services!r}"
        )

    inspected_by_ref: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for item in compose_services:
        container_raw = runner(
            ["docker", "inspect", item["container_name"], "--format", "{{json .}}"],
            repo_root,
        )
        try:
            container = json.loads(container_raw)
        except json.JSONDecodeError as exc:
            raise QualificationError(
                f"invalid docker container inspect JSON for {item['container_name']}"
            ) from exc
        if not isinstance(container, dict):
            raise QualificationError(
                f"docker inspect for container {item['container_name']} is not an object"
            )
        actual_container_id = container.get("Id")
        restart_count = container.get("RestartCount")
        state = container.get("State")
        if not isinstance(actual_container_id, str) or not actual_container_id:
            raise QualificationError(f"container {item['container_name']} missing Id")
        if not isinstance(restart_count, int) or isinstance(restart_count, bool) or restart_count < 0:
            raise QualificationError(f"container {item['container_name']} has invalid RestartCount")
        if not isinstance(state, dict):
            raise QualificationError(f"container {item['container_name']} missing State")
        if state.get("OOMKilled") is not False:
            raise QualificationError(f"container {item['container_name']} was OOM-killed")
        if state.get("Running") is not True:
            raise QualificationError(f"container {item['container_name']} is not running")
        if restart_count != 0:
            raise QualificationError(
                f"container {item['container_name']} restart count is {restart_count}, expected 0"
            )

        image_ref = item["image_ref"]
        if image_ref not in inspected_by_ref:
            raw = runner(["docker", "image", "inspect", image_ref, "--format", "{{json .}}"], repo_root)
            try:
                inspected = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise QualificationError(f"invalid docker image inspect JSON for {image_ref}") from exc
            if not isinstance(inspected, dict):
                raise QualificationError(f"docker image inspect for {image_ref} is not an object")
            inspected_by_ref[image_ref] = inspected

        inspected = inspected_by_ref[image_ref]
        actual_id = inspected.get("Id")
        architecture = inspected.get("Architecture")
        os_name = inspected.get("Os")
        repo_digests = inspected.get("RepoDigests") or []
        if not isinstance(actual_id, str) or not actual_id.startswith("sha256:"):
            raise QualificationError(f"image {image_ref} missing content-addressed image Id")
        if not isinstance(architecture, str) or not architecture:
            raise QualificationError(f"image {image_ref} missing architecture")
        if not isinstance(os_name, str) or not os_name:
            raise QualificationError(f"image {image_ref} missing OS")
        if not isinstance(repo_digests, list) or any(not isinstance(value, str) for value in repo_digests):
            raise QualificationError(f"image {image_ref} has invalid RepoDigests")
        result.append(
            {
                **item,
                "container_id": actual_container_id,
                "restart_count": restart_count,
                "oom_killed": False,
                "image_id": actual_id,
                "architecture": architecture,
                "os": os_name,
                "repo_digests": sorted(repo_digests),
            }
        )
    result.sort(key=lambda item: (item["service"], item["container_name"], item["image_id"]))
    return result


def immutable_write(path: Path, value: dict[str, Any]) -> str:
    raw = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if current is not None:
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise ProvenanceConflict(f"provenance path is not a regular file: {path}")
        existing = path.read_bytes()
        if existing == raw:
            return "replay"
        raise ProvenanceConflict(
            f"immutable provenance conflict at {path}: "
            f"existing_sha256={sha256_bytes(existing)} candidate_sha256={sha256_bytes(raw)}"
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return "created"


def capture(
    *,
    repo_root: Path,
    evidence_dir: Path,
    contract_path: Path,
    compose_path: Path,
    host_sleep_disabled_attested: bool,
    runner: Runner = run_command,
) -> dict[str, str]:
    repo_root = repo_root.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = evidence_dir.resolve()
    contract_path = contract_path.resolve()
    compose_path = compose_path.resolve()

    contract = load_contract(contract_path)
    repository_sha = runner(["git", "rev-parse", "HEAD"], repo_root)
    if not HEX40.fullmatch(repository_sha):
        raise QualificationError(f"git HEAD is not a lowercase 40-hex SHA: {repository_sha!r}")
    dirty = runner(["git", "status", "--porcelain"], repo_root)
    if dirty:
        raise QualificationError("repository worktree must be clean for host qualification")

    architecture = platform.machine().lower()
    vcpu = os.cpu_count() or 0
    memory_total, swap_total = read_meminfo()
    disk = shutil.disk_usage(evidence_dir)
    validate_host_resources(
        contract,
        architecture=architecture,
        vcpu=vcpu,
        memory_total_bytes=memory_total,
        disk_total_bytes=disk.total,
        disk_free_bytes=disk.free,
        host_sleep_disabled_attested=host_sleep_disabled_attested,
    )

    fs_type = runner(["findmnt", "-n", "-o", "FSTYPE", "--target", str(evidence_dir)], repo_root)
    docker_arch = runner(["docker", "info", "--format", "{{.Architecture}}"], repo_root).lower()
    if docker_arch not in contract["allowed_guest_architectures"]:
        raise QualificationError(
            f"Docker architecture {docker_arch!r} not in {contract['allowed_guest_architectures']!r}"
        )

    compose_config = runner(
        ["docker", "compose", "-f", str(compose_path), "config"],
        repo_root,
    )
    expected_services = sorted(
        service
        for service in runner(
            ["docker", "compose", "-f", str(compose_path), "config", "--services"],
            repo_root,
        ).splitlines()
        if service.strip()
    )
    if not expected_services:
        raise QualificationError("docker compose config returned no services")
    compose_ps_raw = runner(
        ["docker", "compose", "-f", str(compose_path), "ps", "--format", "json"],
        repo_root,
    )
    images = inspect_images(parse_compose_ps(compose_ps_raw), expected_services, repo_root, runner)
    if any(image["architecture"].lower() not in contract["allowed_guest_architectures"] for image in images):
        raise QualificationError("one or more runtime images are not ARM64")

    contract_sha = sha256_file(contract_path)
    third_party_lock = repo_root / "payment-reference" / "deploy" / "third-party.lock.json"
    host = {
        "schema": HOST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "repository_sha": repository_sha,
        "host_profile": contract["host_profile"],
        "architecture": architecture,
        "kernel_release": platform.release(),
        "os_release": parse_os_release(),
        "vcpu": vcpu,
        "memory_total_bytes": memory_total,
        "swap_total_bytes": swap_total,
        "filesystem": {
            "path": str(evidence_dir),
            "type": fs_type,
            "total_bytes": disk.total,
            "free_bytes": disk.free,
        },
        "host_sleep_disabled_attested": True,
        "qualification_contract_sha256": contract_sha,
    }
    runtime = {
        "schema": RUNTIME_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "repository_sha": repository_sha,
        "docker_server_version": runner(["docker", "version", "--format", "{{.Server.Version}}"], repo_root),
        "docker_architecture": docker_arch,
        "compose_version": runner(["docker", "compose", "version", "--short"], repo_root),
        "compose_file": str(compose_path.relative_to(repo_root)),
        "compose_config_sha256": sha256_bytes(compose_config.encode("utf-8")),
        "compose_services": expected_services,
        "third_party_lock_sha256": sha256_file(third_party_lock),
        "qualification_contract_sha256": contract_sha,
    }
    image_document = {
        "schema": IMAGES_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "repository_sha": repository_sha,
        "qualification_contract_sha256": contract_sha,
        "images": images,
    }
    toolchains = {
        "schema": TOOLCHAINS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "repository_sha": repository_sha,
        "qualification_contract_sha256": contract_sha,
        "tools": {
            "git": optional_version(["git", "--version"], repo_root, runner),
            "python3": optional_version(["python3", "--version"], repo_root, runner),
            "rustc": optional_version(["rustc", "--version"], repo_root, runner),
            "cargo": optional_version(["cargo", "--version"], repo_root, runner),
            "java": optional_version(["java", "-version"], repo_root, runner),
            "maven": optional_version(["mvn", "-version"], repo_root, runner),
            "docker": runtime["docker_server_version"],
            "docker_compose": runtime["compose_version"],
        },
    }

    documents = {
        "host.json": host,
        "runtime.json": runtime,
        "container-images.json": image_document,
        "toolchains.json": toolchains,
    }
    expected_names = sorted(contract["required_provenance_files"])
    if sorted(documents) != expected_names:
        raise QualificationError("provenance file set disagrees with qualification contract")

    dispositions: dict[str, str] = {}
    for name in expected_names:
        dispositions[name] = immutable_write(evidence_dir / name, documents[name])
    return dispositions


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    deploy_root = here.parent
    parser = argparse.ArgumentParser(description="Capture immutable ARM64 long-running host provenance for campaign #318")
    parser.add_argument("--repo-root", type=Path, default=deploy_root.parent.parent)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=here / "host-qualification-contract.json")
    parser.add_argument("--compose-file", type=Path, default=deploy_root / "compose.yaml")
    parser.add_argument(
        "--host-sleep-disabled-attested",
        action="store_true",
        help="explicit operator attestation that the physical host will not sleep during qualification/campaign execution",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dispositions = capture(
            repo_root=args.repo_root,
            evidence_dir=args.evidence_dir,
            contract_path=args.contract,
            compose_path=args.compose_file,
            host_sleep_disabled_attested=args.host_sleep_disabled_attested,
        )
    except QualificationError as exc:
        print(f"host qualification provenance error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"schema": "oxide-batch-workloads.payment-reference-host-provenance-result", "files": dispositions}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
