#!/usr/bin/env python3
"""#318 per-day read-only quiescence resource sampler.

This module never performs payment, batch, recovery, reconciliation, settlement,
fault-injection, or campaign-frontier mutations. It only observes a quiescent
day-close phase on an already running qualified payment-reference topology and
appends durable resource samples to the campaign samples.jsonl journal.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable

import campaign_core as core

HERE = Path(__file__).resolve().parent
CONTRACT_PATH = HERE / "quiescence-contract-v1.json"
SCHEMA = "oxide-batch-workloads.payment-reference-resource-sample"
SCHEMA_VERSION = 1
MAX_SAMPLE_LINE_BYTES = 262144
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
SAFE_PG_NAME = re.compile(r"[A-Za-z0-9_.-]{1,63}\Z")
SAMPLE_KINDS = {"quiescence-start", "quiescence-periodic", "quiescence-end"}


class QuiescenceError(core.EvidenceError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise QuiescenceError(message)


def _module(name: str, filename: str):
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture = _module("payment_reference_capture_provenance_for_quiescence", "capture-provenance.py")
admission = _module("payment_reference_admission_for_quiescence", "check-admission.py")

Runner = Callable[[list[str], Path | None], str]
ProcessProbe = Callable[[int], dict[str, Any]]
CgroupProbe = Callable[[int], dict[str, Any]]

def postgres_sql(metadata_schema: str) -> str:
    require(
        isinstance(metadata_schema, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", metadata_schema) is not None,
        "unsafe OxideBatch metadata schema",
    )
    return f"""SELECT json_build_object(
  'total_connections', (
    SELECT count(*)::bigint FROM pg_stat_activity WHERE datname = current_database()
  ),
  'active_connections', (
    SELECT count(*)::bigint FROM pg_stat_activity
    WHERE datname = current_database() AND state = 'active'
  ),
  'database_size_bytes', pg_database_size(current_database()),
  'oxide_batch_tables', COALESCE((
    SELECT json_agg(
      json_build_object(
        'schema', schemaname,
        'table', relname,
        'estimated_rows', n_live_tup::bigint,
        'total_bytes', pg_total_relation_size(relid)
      ) ORDER BY relname
    )
    FROM pg_stat_user_tables
    WHERE schemaname = '{metadata_schema}'
  ), '[]'::json)
)::text;"""


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
        raise QuiescenceError(f"unable to execute {command[0]}") from exc
    if completed.returncode != 0:
        raise QuiescenceError(
            f"read-only command failed: executable={command[0]!r} exit={completed.returncode}"
        )
    return completed.stdout.strip()


def load_contract() -> dict[str, Any]:
    value = core.load_json(CONTRACT_PATH)
    expected = {
        "schema", "schema_version", "campaign_contract",
        "sample_schema", "sample_schema_version",
        "target_sample_interval_seconds", "maximum_sample_gap_seconds",
        "minimum_postgres_targets", "oxide_batch_schema",
    }
    require(set(value) == expected, "quiescence contract fields mismatch")
    require(
        value["schema"] == "oxide-batch-workloads.payment-reference-quiescence-contract"
        and value["schema_version"] == 1,
        "quiescence contract schema/version mismatch",
    )
    require(value["campaign_contract"] == core.contract()["contract"],
            "quiescence campaign contract mismatch")
    require(value["sample_schema"] == SCHEMA and value["sample_schema_version"] == SCHEMA_VERSION,
            "quiescence sample schema mismatch")
    target = value["target_sample_interval_seconds"]
    maximum = value["maximum_sample_gap_seconds"]
    require(
        type(target) is int and type(maximum) is int and 1 <= target < maximum <= 5,
        "quiescence sampling cadence must preserve the <=5-second observation contract",
    )
    require(
        type(value["minimum_postgres_targets"]) is int
        and value["minimum_postgres_targets"] > 0,
        "minimum PostgreSQL target count invalid",
    )
    require(
        isinstance(value["oxide_batch_schema"], str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value["oxide_batch_schema"]) is not None,
        "OxideBatch metadata schema invalid",
    )
    return value


def load_campaign(campaign_dir: Path) -> tuple[dict[str, Any], str, str, list[str]]:
    require(campaign_dir.is_dir() and not campaign_dir.is_symlink(),
            "unsafe campaign directory")
    record = core.load_json(campaign_dir / "campaign.json")
    require(
        record.get("schema") == "oxide-batch-workloads.payment-reference-campaign"
        and record.get("schema_version") == 1,
        "campaign schema mismatch",
    )
    require(record.get("contract") == core.contract()["contract"],
            "campaign contract identity mismatch")
    require(record.get("contract_sha256") == core.sha256(core.CONTRACT_PATH),
            "campaign contract digest mismatch")
    identity = record.get("identity")
    require(isinstance(identity, dict), "campaign identity missing")
    campaign_id = identity.get("campaign_id")
    source_revision = identity.get("source_revision")
    images = identity.get("images")
    require(
        campaign_id == campaign_dir.name
        and isinstance(campaign_id, str)
        and core.CAMPAIGN_ID.fullmatch(campaign_id) is not None,
        "campaign directory identity mismatch",
    )
    require(
        isinstance(source_revision, str) and HEX40.fullmatch(source_revision) is not None,
        "campaign source revision invalid",
    )
    require(isinstance(images, dict) and isinstance(images.get("services"), dict),
            "campaign image service identity missing")
    services = sorted(images["services"])
    require(services and all(isinstance(name, str) and name for name in services),
            "campaign service set invalid")
    return record, campaign_id, source_revision, services


def compose_command(compose_file: Path, compose_env_file: Path | None) -> list[str]:
    require(compose_file.is_file() and not compose_file.is_symlink(),
            "Compose file missing or unsafe")
    command = ["docker", "compose"]
    if compose_env_file is not None:
        require(compose_env_file.is_file() and not compose_env_file.is_symlink(),
                "Compose env file missing or unsafe")
        command.extend(["--env-file", str(compose_env_file)])
    command.extend(["-f", str(compose_file)])
    return command


def verify_runtime_binding(
    *,
    campaign_dir: Path,
    source_revision: str,
    expected_services: list[str],
    compose: list[str],
    repo_root: Path,
    runner: Runner,
) -> None:
    runtime = core.load_json(campaign_dir / "provenance" / "runtime.json")
    require(runtime.get("repository_sha") == source_revision,
            "runtime provenance source revision mismatch")
    expected_hash = runtime.get("compose_config_sha256")
    require(isinstance(expected_hash, str) and re.fullmatch(r"[0-9a-f]{64}", expected_hash),
            "runtime provenance Compose digest missing")
    rendered = runner([*compose, "config"], repo_root)
    actual_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    require(actual_hash == expected_hash, "running Compose model differs from qualified provenance")
    services = sorted(
        line.strip()
        for line in runner([*compose, "config", "--services"], repo_root).splitlines()
        if line.strip()
    )
    require(services == expected_services,
            "running Compose service set differs from campaign identity")


def _parse_kb_value(raw: str) -> int:
    parts = raw.strip().split()
    require(len(parts) == 2 and parts[1] == "kB", "unexpected procfs kB field")
    return int(parts[0]) * 1024


def read_process_observation(pid: int) -> dict[str, Any]:
    require(type(pid) is int and pid > 1, "invalid container process PID")
    root = Path("/proc") / str(pid)
    result = {
        "rss_bytes": None,
        "pss_bytes": None,
        "thread_count": None,
        "fd_count": None,
        "status_unavailable_reason": None,
        "smaps_unavailable_reason": None,
        "fd_unavailable_reason": None,
    }
    try:
        status_lines = (root / "status").read_text(encoding="utf-8").splitlines()
        fields = {}
        for line in status_lines:
            name, _, rest = line.partition(":")
            if name in {"VmRSS", "Threads"}:
                fields[name] = rest.strip()
        require("VmRSS" in fields and "Threads" in fields,
                "process status is missing VmRSS/Threads")
        result["rss_bytes"] = _parse_kb_value(fields["VmRSS"])
        result["thread_count"] = int(fields["Threads"])
    except (OSError, ValueError, QuiescenceError) as exc:
        result["status_unavailable_reason"] = type(exc).__name__

    try:
        lines = (root / "smaps_rollup").read_text(encoding="utf-8").splitlines()
        pss = next((line.partition(":")[2] for line in lines if line.startswith("Pss:")), None)
        require(pss is not None, "smaps_rollup missing Pss")
        result["pss_bytes"] = _parse_kb_value(pss)
    except (OSError, ValueError, QuiescenceError) as exc:
        result["smaps_unavailable_reason"] = type(exc).__name__

    try:
        result["fd_count"] = sum(1 for _ in (root / "fd").iterdir())
    except OSError as exc:
        result["fd_unavailable_reason"] = type(exc).__name__
    return result


def read_cgroup_memory(pid: int) -> dict[str, Any]:
    result = {
        "memory_current_bytes": None,
        "memory_peak_bytes": None,
        "unavailable_reason": None,
    }
    try:
        lines = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
        unified = next((line.split("::", 1)[1] for line in lines if line.startswith("0::")), None)
        require(unified is not None, "cgroup v2 unified path unavailable")
        root = Path("/sys/fs/cgroup") / unified.lstrip("/")
        current = (root / "memory.current").read_text(encoding="utf-8").strip()
        peak_path = root / "memory.peak"
        result["memory_current_bytes"] = int(current)
        if peak_path.exists():
            peak = peak_path.read_text(encoding="utf-8").strip()
            result["memory_peak_bytes"] = None if peak == "max" else int(peak)
    except (OSError, ValueError, QuiescenceError) as exc:
        result["unavailable_reason"] = type(exc).__name__
    return result


def _environment_map(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    require(isinstance(value, list), "Compose service environment must be object or list")
    result: dict[str, str] = {}
    for entry in value:
        require(isinstance(entry, str) and "=" in entry,
                "Compose environment list entry must be KEY=VALUE")
        key, raw = entry.split("=", 1)
        result[key] = raw
    return result


def discover_postgres_targets(compose_config: Any) -> list[dict[str, str]]:
    require(isinstance(compose_config, dict), "Compose config must be an object")
    services = compose_config.get("services")
    require(isinstance(services, dict), "Compose config services missing")
    targets: list[dict[str, str]] = []
    for service, definition in services.items():
        require(isinstance(service, str) and isinstance(definition, dict),
                "Compose service definition invalid")
        env = _environment_map(definition.get("environment"))
        database = env.get("POSTGRES_DB", "")
        user = env.get("POSTGRES_USER", "")
        if not database and not user:
            continue
        require(
            SAFE_PG_NAME.fullmatch(database) is not None
            and SAFE_PG_NAME.fullmatch(user) is not None,
            f"{service}: unsafe PostgreSQL database/user identity",
        )
        targets.append({"service": service, "database": database, "user": user})
    targets.sort(key=lambda item: item["service"])
    return targets


def postgres_observation(
    *,
    compose: list[str],
    repo_root: Path,
    target: dict[str, str],
    metadata_schema: str,
    runner: Runner,
) -> dict[str, Any]:
    raw = runner(
        [
            *compose, "exec", "-T", target["service"],
            "psql", "-U", target["user"], "-d", target["database"],
            "-At", "-c", postgres_sql(metadata_schema),
        ],
        repo_root,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QuiescenceError(
            f"{target['service']}: PostgreSQL observation is not JSON"
        ) from exc
    require(isinstance(value, dict), f"{target['service']}: PostgreSQL observation not object")
    for key in ("total_connections", "active_connections", "database_size_bytes"):
        require(type(value.get(key)) is int and value[key] >= 0,
                f"{target['service']}: invalid PostgreSQL metric {key}")
    tables = value.get("oxide_batch_tables")
    require(isinstance(tables, list), f"{target['service']}: metadata table list invalid")
    normalized_tables = []
    for table in tables:
        require(
            isinstance(table, dict)
            and set(table) == {"schema", "table", "estimated_rows", "total_bytes"},
            f"{target['service']}: metadata table observation invalid",
        )
        require(type(table["estimated_rows"]) is int and table["estimated_rows"] >= 0,
                "metadata estimated_rows invalid")
        require(type(table["total_bytes"]) is int and table["total_bytes"] >= 0,
                "metadata total_bytes invalid")
        normalized_tables.append(table)
    return {
        "service": target["service"],
        "database": target["database"],
        "total_connections": value["total_connections"],
        "active_connections": value["active_connections"],
        "database_size_bytes": value["database_size_bytes"],
        "oxide_batch_tables": normalized_tables,
    }


def _filesystem(role: str, path: Path, disk_reader: Callable[[Path], Any]) -> dict[str, Any]:
    usage = disk_reader(path)
    return {
        "role": role,
        "path": str(path),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "available_bytes": int(usage.free),
    }


def collect_observation(
    *,
    campaign_dir: Path,
    data_path: Path,
    expected_services: list[str],
    contract: dict[str, Any],
    compose: list[str],
    repo_root: Path,
    runner: Runner = run_command,
    meminfo_reader: Callable[[], dict[str, int]] = admission.read_meminfo,
    load_reader: Callable[[], tuple[float, float, float]] = os.getloadavg,
    disk_reader: Callable[[Path], Any] = shutil.disk_usage,
    process_probe: ProcessProbe = read_process_observation,
    cgroup_probe: CgroupProbe = read_cgroup_memory,
) -> dict[str, Any]:
    mem = meminfo_reader()
    load = load_reader()
    require(len(load) == 3, "load average observation malformed")
    swap_used = max(0, mem["SwapTotal"] - mem["SwapFree"])

    ps_raw = runner([*compose, "ps", "--format", "json"], repo_root)
    compose_rows = capture.parse_compose_ps(ps_raw)
    observed_services = sorted(row["service"] for row in compose_rows)
    require(observed_services == expected_services,
            "running Compose service set differs from campaign identity")

    names = [row["container_name"] for row in compose_rows]
    inspect_raw = runner(["docker", "inspect", *names], repo_root)
    try:
        inspected = json.loads(inspect_raw)
    except json.JSONDecodeError as exc:
        raise QuiescenceError("docker inspect returned invalid JSON") from exc
    if isinstance(inspected, dict):
        inspected = [inspected]
    require(isinstance(inspected, list), "docker inspect result must be an array")
    by_name: dict[str, dict[str, Any]] = {}
    for value in inspected:
        require(isinstance(value, dict), "docker inspect entry not object")
        name = value.get("Name")
        require(isinstance(name, str) and name, "docker inspect entry missing Name")
        by_name[name.lstrip("/")] = value

    containers = []
    for row in compose_rows:
        item = by_name.get(row["container_name"])
        require(isinstance(item, dict), f"{row['service']}: docker inspect entry missing")
        state = item.get("State")
        restart_count = item.get("RestartCount")
        require(isinstance(state, dict), f"{row['service']}: container state missing")
        require(type(restart_count) is int and restart_count >= 0,
                f"{row['service']}: restart count invalid")
        require(state.get("Running") is True, f"{row['service']}: container not running")
        require(state.get("OOMKilled") is False, f"{row['service']}: container was OOM-killed")
        require(restart_count == 0,
                f"{row['service']}: unexpected container restart count {restart_count}")
        pid = state.get("Pid")
        require(type(pid) is int and pid > 1, f"{row['service']}: container PID invalid")
        container_id = item.get("Id")
        require(isinstance(container_id, str) and container_id,
                f"{row['service']}: container ID missing")
        containers.append({
            "service": row["service"],
            "container_name": row["container_name"],
            "container_id": container_id,
            "pid": pid,
            "restart_count": restart_count,
            "oom_killed": False,
            "running": True,
            "process": process_probe(pid),
            "cgroup_memory": cgroup_probe(pid),
        })
    containers.sort(key=lambda item: item["service"])

    config_raw = runner([*compose, "config", "--format", "json"], repo_root)
    try:
        config = json.loads(config_raw)
    except json.JSONDecodeError as exc:
        raise QuiescenceError("docker compose config JSON invalid") from exc
    targets = discover_postgres_targets(config)
    require(
        len(targets) >= contract["minimum_postgres_targets"],
        "qualified topology exposes fewer PostgreSQL targets than the quiescence contract",
    )
    postgres = [
        postgres_observation(
            compose=compose,
            repo_root=repo_root,
            target=target,
            metadata_schema=contract["oxide_batch_schema"],
            runner=runner,
        )
        for target in targets
    ]

    return {
        "host": {
            "memory_total_bytes": mem["MemTotal"],
            "memory_available_bytes": mem["MemAvailable"],
            "swap_total_bytes": mem["SwapTotal"],
            "swap_used_bytes": swap_used,
            "load_average_1m": float(load[0]),
            "load_average_5m": float(load[1]),
            "load_average_15m": float(load[2]),
        },
        "filesystems": [
            _filesystem("campaign-evidence", campaign_dir, disk_reader),
            _filesystem("campaign-data", data_path, disk_reader),
        ],
        "containers": containers,
        "postgres": postgres,
    }


def sample_record(
    *,
    campaign_id: str,
    sequence: int,
    source_revision: str,
    contract: dict[str, Any],
    contract_sha256: str,
    kind: str,
    window_sample_index: int,
    timestamp_unix_nanos_utc: int,
    monotonic_elapsed_nanos: int,
    observation: dict[str, Any],
) -> dict[str, Any]:
    required_seconds = core.plan_day(sequence)["minimum_quiet_seconds"]
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "ordinal": 0,
        "quiescence_contract_sha256": contract_sha256,
        "campaign_id": campaign_id,
        "logical_day_sequence": sequence,
        "source_revision": source_revision,
        "window_id": f"{campaign_id}:day-{sequence:06}:quiescence",
        "window_sample_index": window_sample_index,
        "sample_kind": kind,
        "timestamp_unix_nanos_utc": timestamp_unix_nanos_utc,
        "monotonic_elapsed_nanos": monotonic_elapsed_nanos,
        "required_quiescence_seconds": required_seconds,
        "target_sample_interval_seconds": contract["target_sample_interval_seconds"],
        "observation": observation,
    }


def validate_sample(
    value: Any,
    *,
    campaign_id: str,
    sequence: int,
    source_revision: str,
    contract: dict[str, Any],
    contract_sha256: str,
    expected_services: list[str],
) -> None:
    require(isinstance(value, dict), "resource sample must be an object")
    expected_fields = {
        "schema", "schema_version", "ordinal", "quiescence_contract_sha256",
        "campaign_id", "logical_day_sequence", "source_revision", "window_id",
        "window_sample_index", "sample_kind", "timestamp_unix_nanos_utc",
        "monotonic_elapsed_nanos", "required_quiescence_seconds",
        "target_sample_interval_seconds", "observation",
    }
    require(set(value) == expected_fields, "resource sample fields mismatch")
    require(value["schema"] == SCHEMA and value["schema_version"] == SCHEMA_VERSION,
            "resource sample schema/version mismatch")
    require(type(value["ordinal"]) is int and value["ordinal"] > 0,
            "resource sample ordinal invalid")
    require(value["quiescence_contract_sha256"] == contract_sha256,
            "quiescence contract digest mismatch")
    require(
        value["campaign_id"] == campaign_id
        and value["logical_day_sequence"] == sequence
        and value["source_revision"] == source_revision,
        "resource sample campaign/day/source mismatch",
    )
    require(
        value["window_id"] == f"{campaign_id}:day-{sequence:06}:quiescence",
        "quiescence window identity mismatch",
    )
    require(type(value["window_sample_index"]) is int and value["window_sample_index"] >= 0,
            "quiescence window sample index invalid")
    require(value["sample_kind"] in SAMPLE_KINDS, "quiescence sample kind invalid")
    require(type(value["timestamp_unix_nanos_utc"]) is int and value["timestamp_unix_nanos_utc"] > 0,
            "resource sample timestamp invalid")
    require(type(value["monotonic_elapsed_nanos"]) is int and value["monotonic_elapsed_nanos"] >= 0,
            "resource sample elapsed time invalid")
    require(value["required_quiescence_seconds"] == core.plan_day(sequence)["minimum_quiet_seconds"],
            "resource sample required quiescence duration mismatch")
    require(value["target_sample_interval_seconds"] == contract["target_sample_interval_seconds"],
            "resource sample interval mismatch")

    observation = value["observation"]
    require(isinstance(observation, dict)
            and set(observation) == {"host", "filesystems", "containers", "postgres"},
            "resource observation fields mismatch")
    host = observation["host"]
    require(isinstance(host, dict), "host observation missing")
    for key in (
        "memory_total_bytes", "memory_available_bytes",
        "swap_total_bytes", "swap_used_bytes",
    ):
        require(type(host.get(key)) is int and host[key] >= 0,
                f"host metric {key} invalid")
    for key in ("load_average_1m", "load_average_5m", "load_average_15m"):
        require(type(host.get(key)) in {int, float} and host[key] >= 0,
                f"host metric {key} invalid")

    filesystems = observation["filesystems"]
    require(isinstance(filesystems, list)
            and {item.get("role") for item in filesystems if isinstance(item, dict)}
            == {"campaign-evidence", "campaign-data"},
            "filesystem observation roles mismatch")
    for item in filesystems:
        require(isinstance(item, dict)
                and set(item) == {"role", "path", "total_bytes", "used_bytes", "available_bytes"},
                "filesystem observation invalid")
        for key in ("total_bytes", "used_bytes", "available_bytes"):
            require(type(item[key]) is int and item[key] >= 0,
                    f"filesystem metric {key} invalid")

    containers = observation["containers"]
    require(isinstance(containers, list), "container observation list missing")
    require(sorted(item.get("service") for item in containers if isinstance(item, dict))
            == expected_services,
            "container observation service set mismatch")
    for item in containers:
        require(isinstance(item, dict), "container observation invalid")
        require(item.get("running") is True and item.get("oom_killed") is False,
                f"{item.get('service')}: container not healthy for quiescence evidence")
        require(item.get("restart_count") == 0,
                f"{item.get('service')}: restarted container cannot be accepted")

    postgres = observation["postgres"]
    require(isinstance(postgres, list)
            and len(postgres) >= contract["minimum_postgres_targets"],
            "PostgreSQL observation target count mismatch")
    services = [item.get("service") for item in postgres if isinstance(item, dict)]
    require(len(services) == len(set(services)), "duplicate PostgreSQL observation target")
    for item in postgres:
        require(isinstance(item, dict), "PostgreSQL observation invalid")
        for key in ("total_connections", "active_connections", "database_size_bytes"):
            require(type(item.get(key)) is int and item[key] >= 0,
                    f"PostgreSQL metric {key} invalid")
        require(isinstance(item.get("oxide_batch_tables"), list),
                "OxideBatch metadata table observation missing")


def _last_ordinal(file, campaign_id: str) -> int:
    file.seek(0, os.SEEK_END)
    size = file.tell()
    if size == 0:
        return 0
    window = min(size, MAX_SAMPLE_LINE_BYTES + 1)
    file.seek(size - window)
    tail = file.read(window)
    require(tail.endswith(b"\n"), "samples.jsonl has a torn terminal record")
    cut = tail.rfind(b"\n", 0, len(tail) - 1)
    require(cut >= 0 or size <= window, "samples.jsonl terminal record oversized")
    raw = tail[cut + 1:-1]
    require(len(raw) <= MAX_SAMPLE_LINE_BYTES, "samples.jsonl terminal record oversized")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QuiescenceError("samples.jsonl terminal record invalid JSON") from exc
    require(
        isinstance(value, dict)
        and value.get("schema") == SCHEMA
        and value.get("schema_version") == SCHEMA_VERSION
        and value.get("campaign_id") == campaign_id
        and type(value.get("ordinal")) is int
        and value["ordinal"] > 0,
        "samples.jsonl terminal record identity invalid",
    )
    return value["ordinal"]


def append_sample(
    campaign_dir: Path,
    value: dict[str, Any],
    *,
    campaign_id: str,
    sequence: int,
    source_revision: str,
    contract: dict[str, Any],
    contract_sha256: str,
    expected_services: list[str],
) -> dict[str, Any]:
    path = campaign_dir / "samples.jsonl"
    current = path.lstat()
    require(stat.S_ISREG(current.st_mode) and not stat.S_ISLNK(current.st_mode),
            "samples.jsonl must be a regular non-symlink file")
    with path.open("r+b") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        candidate = dict(value)
        candidate["ordinal"] = _last_ordinal(file, campaign_id) + 1
        validate_sample(
            candidate,
            campaign_id=campaign_id,
            sequence=sequence,
            source_revision=source_revision,
            contract=contract,
            contract_sha256=contract_sha256,
            expected_services=expected_services,
        )
        raw = json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8") + b"\n"
        require(len(raw) <= MAX_SAMPLE_LINE_BYTES, "resource sample record too large")
        file.seek(0, os.SEEK_END)
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())
    return candidate


def read_window_records(
    campaign_dir: Path,
    *,
    campaign_id: str,
    sequence: int,
) -> list[dict[str, Any]]:
    path = campaign_dir / "samples.jsonl"
    window_id = f"{campaign_id}:day-{sequence:06}:quiescence"
    values = []
    with path.open("rb") as source:
        while raw := source.readline(MAX_SAMPLE_LINE_BYTES + 2):
            require(len(raw) <= MAX_SAMPLE_LINE_BYTES + 1 and raw.endswith(b"\n"),
                    "samples.jsonl contains oversized or torn record")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise QuiescenceError("samples.jsonl contains invalid JSON") from exc
            require(
                isinstance(value, dict)
                and value.get("schema") == SCHEMA
                and value.get("schema_version") == SCHEMA_VERSION
                and value.get("campaign_id") == campaign_id,
                "samples.jsonl contains foreign sample schema/campaign",
            )
            if value.get("window_id") == window_id:
                values.append(value)
    return values


def verify_window(
    records: list[dict[str, Any]],
    *,
    campaign_id: str,
    sequence: int,
    source_revision: str,
    contract: dict[str, Any],
    contract_sha256: str,
    expected_services: list[str],
) -> dict[str, Any]:
    require(records, "quiescence window has no retained samples")
    for value in records:
        validate_sample(
            value,
            campaign_id=campaign_id,
            sequence=sequence,
            source_revision=source_revision,
            contract=contract,
            contract_sha256=contract_sha256,
            expected_services=expected_services,
        )
    require(records[0]["sample_kind"] == "quiescence-start",
            "quiescence window does not start with start marker")
    require(records[-1]["sample_kind"] == "quiescence-end",
            "quiescence window does not end with end marker")
    require(records[0]["monotonic_elapsed_nanos"] == 0,
            "quiescence start elapsed time must be zero")

    previous_elapsed = -1
    previous_timestamp = -1
    postgres_services = None
    maximum_gap = 0
    for index, value in enumerate(records):
        require(value["window_sample_index"] == index,
                "quiescence sample indexes are not contiguous")
        if 0 < index < len(records) - 1:
            require(value["sample_kind"] == "quiescence-periodic",
                    "interior quiescence sample must be periodic")
        elapsed = value["monotonic_elapsed_nanos"]
        timestamp = value["timestamp_unix_nanos_utc"]
        require(elapsed >= previous_elapsed and timestamp >= previous_timestamp,
                "quiescence sample time moved backward")
        if previous_elapsed >= 0:
            gap = elapsed - previous_elapsed
            maximum_gap = max(maximum_gap, gap)
            require(
                gap <= contract["maximum_sample_gap_seconds"] * 1_000_000_000,
                "quiescence sampling gap exceeded contract",
            )
        previous_elapsed = elapsed
        previous_timestamp = timestamp
        observed_pg = tuple(sorted(item["service"] for item in value["observation"]["postgres"]))
        if postgres_services is None:
            postgres_services = observed_pg
        else:
            require(observed_pg == postgres_services,
                    "PostgreSQL observation target set drifted during quiescence")

    required_nanos = records[0]["required_quiescence_seconds"] * 1_000_000_000
    require(records[-1]["monotonic_elapsed_nanos"] >= required_nanos,
            "quiescence window ended before required duration")
    return {
        "window_id": records[0]["window_id"],
        "sample_count": len(records),
        "first_ordinal": records[0]["ordinal"],
        "last_ordinal": records[-1]["ordinal"],
        "observed_duration_nanos": records[-1]["monotonic_elapsed_nanos"],
        "maximum_sample_gap_nanos": maximum_gap,
        "postgres_services": list(postgres_services or ()),
    }


def run_window(
    *,
    campaign_dir: Path,
    campaign_id: str,
    sequence: int,
    source_revision: str,
    expected_services: list[str],
    contract: dict[str, Any],
    observation_factory: Callable[[], dict[str, Any]],
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    time_ns: Callable[[], int] = time.time_ns,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    contract_sha = core.sha256(CONTRACT_PATH)
    existing = read_window_records(
        campaign_dir, campaign_id=campaign_id, sequence=sequence
    )
    if existing:
        subject = verify_window(
            existing,
            campaign_id=campaign_id,
            sequence=sequence,
            source_revision=source_revision,
            contract=contract,
            contract_sha256=contract_sha,
            expected_services=expected_services,
        )
        return {"retention": "replay", **subject}

    duration_seconds = core.plan_day(sequence)["minimum_quiet_seconds"]
    interval_seconds = contract["target_sample_interval_seconds"]
    start = monotonic_ns()
    index = 0
    target_seconds = 0

    while True:
        target_nanos = start + target_seconds * 1_000_000_000
        now = monotonic_ns()
        while now < target_nanos:
            sleep((target_nanos - now) / 1_000_000_000)
            now = monotonic_ns()

        kind = (
            "quiescence-start"
            if target_seconds == 0
            else "quiescence-end"
            if target_seconds == duration_seconds
            else "quiescence-periodic"
        )
        elapsed = 0 if index == 0 else max(0, now - start)
        observation = observation_factory()
        value = sample_record(
            campaign_id=campaign_id,
            sequence=sequence,
            source_revision=source_revision,
            contract=contract,
            contract_sha256=contract_sha,
            kind=kind,
            window_sample_index=index,
            timestamp_unix_nanos_utc=time_ns(),
            monotonic_elapsed_nanos=elapsed,
            observation=observation,
        )
        append_sample(
            campaign_dir,
            value,
            campaign_id=campaign_id,
            sequence=sequence,
            source_revision=source_revision,
            contract=contract,
            contract_sha256=contract_sha,
            expected_services=expected_services,
        )
        if target_seconds == duration_seconds:
            break
        target_seconds = min(duration_seconds, target_seconds + interval_seconds)
        index += 1

    records = read_window_records(
        campaign_dir, campaign_id=campaign_id, sequence=sequence
    )
    subject = verify_window(
        records,
        campaign_id=campaign_id,
        sequence=sequence,
        source_revision=source_revision,
        contract=contract,
        contract_sha256=contract_sha,
        expected_services=expected_services,
    )
    return {"retention": "created", **subject}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retain one logical day's read-only quiescence resource evidence"
    )
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--data-path", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--compose-env-file", type=Path)
    parser.add_argument("--day", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        require(type(args.day) is int and args.day > 0, "day must be positive")
        campaign_dir = args.campaign_dir.resolve()
        repo_root = args.repo_root.resolve()
        data_path = args.data_path.resolve()
        compose_file = args.compose_file.resolve()
        compose_env_file = (
            args.compose_env_file.resolve() if args.compose_env_file is not None else None
        )
        require(repo_root.is_dir() and data_path.is_dir(),
                "repo-root and data-path must be existing directories")

        contract = load_contract()
        _, campaign_id, source_revision, expected_services = load_campaign(campaign_dir)
        head = run_command(["git", "rev-parse", "HEAD"], repo_root)
        require(head == source_revision,
                "repository HEAD differs from frozen campaign source revision")
        require(not run_command(["git", "status", "--porcelain"], repo_root),
                "repository worktree must be clean during quiescence evidence collection")

        compose = compose_command(compose_file, compose_env_file)
        verify_runtime_binding(
            campaign_dir=campaign_dir,
            source_revision=source_revision,
            expected_services=expected_services,
            compose=compose,
            repo_root=repo_root,
            runner=run_command,
        )
        result = run_window(
            campaign_dir=campaign_dir,
            campaign_id=campaign_id,
            sequence=args.day,
            source_revision=source_revision,
            expected_services=expected_services,
            contract=contract,
            observation_factory=lambda: collect_observation(
                campaign_dir=campaign_dir,
                data_path=data_path,
                expected_services=expected_services,
                contract=contract,
                compose=compose,
                repo_root=repo_root,
                runner=run_command,
            ),
        )
        print(json.dumps(
            {
                "decision": "QUIESCENCE_VERIFIED_NOT_DAY_ACCEPTED",
                "logical_day_sequence": args.day,
                **result,
            },
            sort_keys=True,
        ))
        return 0
    except (
        QuiescenceError,
        capture.QualificationError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(
            json.dumps(
                {"decision": "BLOCK", "error_type": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
