#!/usr/bin/env python3
"""Qualify the stable OCI ARM64 host for allocator campaign #323."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import urllib.request
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
WORKLOAD_ROOT = HERE.parent
REPOSITORY_ROOT = WORKLOAD_ROOT.parent
CONTRACT_PATH = HERE / "oci_host_qualification_contract.json"
IMDS_BASE = "http://169.254.169.254/opc/v2/instance"


class QualificationError(RuntimeError):
    pass


def run(command: list[str], *, cwd: pathlib.Path = WORKLOAD_ROOT) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise QualificationError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-4000:]}"
        )
    return completed.stdout.strip()


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer Oracle"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read()
    except Exception as exc:
        raise QualificationError(f"OCI IMDS request failed: {url}: {exc}") from exc
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise QualificationError(f"OCI IMDS returned invalid JSON: {url}") from exc
    if not isinstance(document, dict):
        raise QualificationError(f"OCI IMDS returned non-object JSON: {url}")
    return document


def meminfo_value_bytes(name: str) -> int:
    for line in pathlib.Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        if key == name:
            parts = rest.strip().split()
            if len(parts) != 2 or parts[1] != "kB":
                raise QualificationError(f"unexpected /proc/meminfo format for {name}")
            return int(parts[0]) * 1024
    raise QualificationError(f"missing /proc/meminfo field: {name}")


def os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in pathlib.Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip().strip('"')
    return result


def root_disk() -> dict[str, Any]:
    fs_type = run(["findmnt", "-n", "-o", "FSTYPE", "/"])
    output = run(["df", "-B1", "--output=size,avail", "/"]).splitlines()
    if len(output) != 2:
        raise QualificationError(f"unexpected df output: {output!r}")
    total, available = [int(value) for value in output[1].split()]
    return {
        "filesystem": fs_type,
        "total_bytes": total,
        "available_bytes": available,
    }


def cpu_info() -> dict[str, Any]:
    raw = pathlib.Path("/proc/cpuinfo").read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = [part.strip() for part in line.split(":", 1)]
        if key not in fields and key in {
            "model name",
            "CPU implementer",
            "CPU architecture",
            "CPU part",
            "Hardware",
        }:
            fields[key] = value
    governors: list[str] = []
    for path in sorted(pathlib.Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")):
        try:
            governors.append(path.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return {
        "logical_cpu_count": os.cpu_count(),
        "fields": fields,
        "scaling_governors": sorted(set(governors)),
    }


def validate_host(
    contract: dict[str, Any],
    instance: dict[str, Any],
    shape: dict[str, Any],
    disk: dict[str, Any],
    swap_bytes: int,
) -> None:
    if platform.system() != "Linux":
        raise QualificationError("host must be Linux")
    if platform.machine().lower() != contract["host"]["required_machine"]:
        raise QualificationError(
            f"machine must be {contract['host']['required_machine']}: {platform.machine()}"
        )
    if instance.get("shape") != contract["oci"]["required_shape"]:
        raise QualificationError(
            f"OCI shape must be {contract['oci']['required_shape']}: {instance.get('shape')}"
        )

    ocpus = float(shape.get("ocpus", 0))
    memory_gbs = float(shape.get("memoryInGBs", 0))
    if ocpus < contract["oci"]["min_ocpus"]:
        raise QualificationError(f"OCI OCPUs below contract: {ocpus}")
    if memory_gbs < contract["oci"]["min_memory_gbs"]:
        raise QualificationError(f"OCI memory below contract: {memory_gbs}")

    if disk["filesystem"] != contract["host"]["required_root_filesystem"]:
        raise QualificationError(
            f"root filesystem must be {contract['host']['required_root_filesystem']}: "
            f"{disk['filesystem']}"
        )
    if disk["total_bytes"] < contract["host"]["min_root_disk_bytes"]:
        raise QualificationError(
            f"root disk below contract: {disk['total_bytes']}"
        )
    if swap_bytes > contract["host"]["max_swap_bytes"]:
        raise QualificationError(f"swap must remain disabled: {swap_bytes} bytes")


def validate_repository(expected_sha: str) -> None:
    if len(expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in expected_sha):
        raise QualificationError("--expected-sha must be exactly 40 lowercase hex characters")
    actual = run(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT)
    if actual != expected_sha:
        raise QualificationError(f"repository SHA mismatch: expected={expected_sha} actual={actual}")
    dirty = run(["git", "status", "--porcelain"], cwd=REPOSITORY_ROOT)
    if dirty:
        raise QualificationError("repository worktree must be clean")


def validate_smoke_outputs(root: pathlib.Path, contract: dict[str, Any]) -> None:
    expected = contract["allocators"]
    observed = sorted(path.name for path in root.iterdir() if path.is_dir())
    if observed != sorted(expected):
        raise QualificationError(
            f"allocator smoke set mismatch: expected={sorted(expected)} observed={observed}"
        )
    for allocator in expected:
        manifest_path = root / allocator / "manifest.json"
        if not manifest_path.is_file():
            raise QualificationError(f"missing allocator manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("campaign") != contract["campaign"]:
            raise QualificationError(f"{allocator}: wrong campaign")
        oxide = manifest.get("subject", {}).get("oxide_batch", {})
        if oxide.get("version") != contract["subject"]["version"]:
            raise QualificationError(f"{allocator}: wrong OxideBatch version")
        if oxide.get("checksum") != contract["subject"]["checksum"]:
            raise QualificationError(f"{allocator}: wrong OxideBatch checksum")
        if manifest.get("allocator", {}).get("identity") != allocator:
            raise QualificationError(f"{allocator}: allocator identity mismatch")
        if manifest.get("correctness", {}).get("verdict") != "PASS":
            raise QualificationError(f"{allocator}: correctness did not PASS")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument(
        "--base-database-url",
        default="postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5434/postgres_postgres_workload",
    )
    args = parser.parse_args()

    if args.output.exists():
        raise QualificationError(f"output already exists: {args.output}")
    args.output.mkdir(parents=True)
    smoke_root = args.output / "smoke"

    contract = load_contract()
    validate_repository(args.expected_sha)

    instance = fetch_json(f"{IMDS_BASE}/")
    shape = fetch_json(f"{IMDS_BASE}/shapeConfig")
    disk = root_disk()
    swap_bytes = meminfo_value_bytes("SwapTotal")
    validate_host(contract, instance, shape, disk, swap_bytes)

    provenance = {
        "schema": "oxide-batch-allocator-oci-host-provenance/v1",
        "campaign": contract["campaign"],
        "repository_sha": args.expected_sha,
        "oci": {
            "instance_id": instance.get("id"),
            "display_name": instance.get("displayName"),
            "region": instance.get("region"),
            "availability_domain": instance.get("availabilityDomain"),
            "fault_domain": instance.get("faultDomain"),
            "shape": instance.get("shape"),
            "shape_config": shape,
        },
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "kernel": platform.release(),
            "os_release": os_release(),
            "mem_total_bytes": meminfo_value_bytes("MemTotal"),
            "swap_total_bytes": swap_bytes,
            "root_disk": disk,
            "cpu": cpu_info(),
        },
        "toolchain": {
            "rustc": run(["rustc", "-vV"]),
            "cargo": run(["cargo", "--version"]),
            "python": sys.version,
            "git": run(["git", "--version"]),
            "docker": run(["docker", "--version"]),
            "docker_compose": run(["docker", "compose", "version"]),
        },
        "claim_boundary": contract["claim_boundary"],
    }
    (args.output / "host.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    compose_started = False
    try:
        run(["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "60"])
        compose_started = True
        postgres_version = run([
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "oxide_batch_workload",
            "-d",
            "postgres_postgres_workload",
            "-Atqc",
            "select version()",
        ])
        provenance["postgres"] = {
            "configured_images": run(["docker", "compose", "config", "--images"]).splitlines(),
            "server_version": postgres_version,
        }
        (args.output / "host.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        smoke = contract["smoke"]
        run([
            sys.executable,
            "benchmark/profile_smoke.py",
            "--base-database-url",
            args.base_database_url,
            "--output",
            str(smoke_root),
            "--rows",
            str(smoke["rows"]),
            "--seed",
            str(smoke["seed"]),
            "--chunk-size",
            str(smoke["chunk_size"]),
            "--reader",
            smoke["reader"],
            "--fetch-size",
            str(smoke["fetch_size"]),
            "--page-size",
            str(smoke["page_size"]),
        ])
        validate_smoke_outputs(smoke_root, contract)
    finally:
        if compose_started:
            subprocess.run(
                ["docker", "compose", "down", "--volumes"],
                cwd=WORKLOAD_ROOT,
                check=False,
            )

    qualification = {
        "schema": "oxide-batch-allocator-oci-host-qualification/v1",
        "campaign": contract["campaign"],
        "verdict": "PASS",
        "repository_sha": args.expected_sha,
        "host_sha256": sha256_file(args.output / "host.json"),
        "allocators": contract["allocators"],
        "smoke": contract["smoke"],
        "claim": "host-qualification-and-correctness-only",
        "limitations": contract["claim_boundary"],
    }
    (args.output / "qualification.json").write_text(
        json.dumps(qualification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(qualification, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QualificationError as exc:
        raise SystemExit(f"OCI allocator host qualification failed: {exc}") from exc
