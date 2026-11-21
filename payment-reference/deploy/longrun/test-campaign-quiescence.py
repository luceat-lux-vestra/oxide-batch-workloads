#!/usr/bin/env python3
"""Offline #318 quiescence resource-sampler tests."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import campaign_core as core

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "campaign_quiescence", HERE / "campaign_quiescence.py"
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
q = module

SHA40 = "a" * 40
SERVICES = sorted((
    "merchant", "gateway", "killbill", "payment-sandbox",
    "payment-sandbox-fault-proxy", "merchant-db", "gateway-db", "killbill-db",
))


def observation():
    return {
        "host": {
            "memory_total_bytes": 12 * 1024**3,
            "memory_available_bytes": 8 * 1024**3,
            "swap_total_bytes": 2 * 1024**3,
            "swap_used_bytes": 0,
            "load_average_1m": 0.1,
            "load_average_5m": 0.2,
            "load_average_15m": 0.3,
        },
        "filesystems": [
            {
                "role": "campaign-evidence", "path": "/evidence",
                "total_bytes": 1000, "used_bytes": 100, "available_bytes": 900,
            },
            {
                "role": "campaign-data", "path": "/data",
                "total_bytes": 2000, "used_bytes": 200, "available_bytes": 1800,
            },
        ],
        "containers": [
            {
                "service": service,
                "container_name": f"arbitrary-{service}-7",
                "container_id": f"id-{service}",
                "pid": 1000 + index,
                "restart_count": 0,
                "oom_killed": False,
                "running": True,
                "process": {
                    "rss_bytes": 1,
                    "pss_bytes": 1,
                    "thread_count": 1,
                    "fd_count": 1,
                    "status_unavailable_reason": None,
                    "smaps_unavailable_reason": None,
                    "fd_unavailable_reason": None,
                },
                "cgroup_memory": {
                    "memory_current_bytes": 1,
                    "memory_peak_bytes": 2,
                    "unavailable_reason": None,
                },
            }
            for index, service in enumerate(SERVICES)
        ],
        "postgres": [
            {
                "service": service,
                "database": service.removesuffix("-db"),
                "total_connections": 2,
                "active_connections": 1,
                "database_size_bytes": 4096,
                "oxide_batch_tables": [],
            }
            for service in ("gateway-db", "killbill-db", "merchant-db")
        ],
    }


class FakeClock:
    def __init__(self):
        self.now = 0
        self.epoch = 1_800_000_000_000_000_000

    def monotonic_ns(self):
        return self.now

    def time_ns(self):
        return self.epoch + self.now

    def sleep(self, seconds):
        self.now += int(seconds * 1_000_000_000)


class QuiescenceTests(unittest.TestCase):
    def test_contract_freezes_sampling_cadence_without_changing_campaign_contract(self):
        contract = q.load_contract()
        self.assertEqual(contract["campaign_contract"], core.contract()["contract"])
        self.assertEqual(contract["target_sample_interval_seconds"], 4)
        self.assertEqual(contract["maximum_sample_gap_seconds"], 5)
        self.assertEqual(contract["minimum_postgres_targets"], 3)
        self.assertEqual(contract["sample_schema"], q.SCHEMA)

    def test_postgres_targets_are_discovered_from_resolved_compose_environment(self):
        config = {
            "services": {
                "merchant-db": {
                    "environment": {"POSTGRES_DB": "merchant", "POSTGRES_USER": "merchant"}
                },
                "gateway-db": {
                    "environment": ["POSTGRES_DB=gateway", "POSTGRES_USER=gateway"]
                },
                "killbill-db": {
                    "environment": {"POSTGRES_DB": "killbill", "POSTGRES_USER": "killbill"}
                },
                "merchant": {"environment": {"OTHER": "value"}},
            }
        }
        self.assertEqual(
            q.discover_postgres_targets(config),
            [
                {"service": "gateway-db", "database": "gateway", "user": "gateway"},
                {"service": "killbill-db", "database": "killbill", "user": "killbill"},
                {"service": "merchant-db", "database": "merchant", "user": "merchant"},
            ],
        )

    def test_run_window_appends_60_second_day_samples_and_exact_replay_is_read_only(self):
        contract = q.load_contract()
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "samples.jsonl").write_bytes(b"")
            first = q.run_window(
                campaign_dir=root,
                campaign_id="test318",
                sequence=1,
                source_revision=SHA40,
                expected_services=SERVICES,
                contract=contract,
                observation_factory=lambda: copy.deepcopy(observation()),
                monotonic_ns=clock.monotonic_ns,
                time_ns=clock.time_ns,
                sleep=clock.sleep,
            )
            self.assertEqual(first["retention"], "created")
            self.assertEqual(first["observed_duration_nanos"], 60 * 1_000_000_000)
            self.assertLessEqual(
                first["maximum_sample_gap_nanos"],
                contract["maximum_sample_gap_seconds"] * 1_000_000_000,
            )
            before = (root / "samples.jsonl").read_bytes()
            replay = q.run_window(
                campaign_dir=root,
                campaign_id="test318",
                sequence=1,
                source_revision=SHA40,
                expected_services=SERVICES,
                contract=contract,
                observation_factory=lambda: self.fail("replay must not collect again"),
                monotonic_ns=clock.monotonic_ns,
                time_ns=clock.time_ns,
                sleep=clock.sleep,
            )
            self.assertEqual(replay["retention"], "replay")
            self.assertEqual((root / "samples.jsonl").read_bytes(), before)

    def test_incomplete_or_sparse_window_fails_closed(self):
        contract = q.load_contract()
        contract_sha = core.sha256(q.CONTRACT_PATH)
        start = q.sample_record(
            campaign_id="test318",
            sequence=1,
            source_revision=SHA40,
            contract=contract,
            contract_sha256=contract_sha,
            kind="quiescence-start",
            window_sample_index=0,
            timestamp_unix_nanos_utc=1,
            monotonic_elapsed_nanos=0,
            observation=observation(),
        )
        start["ordinal"] = 1
        periodic = q.sample_record(
            campaign_id="test318",
            sequence=1,
            source_revision=SHA40,
            contract=contract,
            contract_sha256=contract_sha,
            kind="quiescence-periodic",
            window_sample_index=1,
            timestamp_unix_nanos_utc=2,
            monotonic_elapsed_nanos=6 * 1_000_000_000,
            observation=observation(),
        )
        periodic["ordinal"] = 2
        with self.assertRaisesRegex(q.QuiescenceError, "does not end"):
            q.verify_window(
                [start],
                campaign_id="test318", sequence=1, source_revision=SHA40,
                contract=contract, contract_sha256=contract_sha,
                expected_services=SERVICES,
            )
        end = copy.deepcopy(periodic)
        end["sample_kind"] = "quiescence-end"
        end["monotonic_elapsed_nanos"] = 60 * 1_000_000_000
        with self.assertRaisesRegex(q.QuiescenceError, "sampling gap exceeded"):
            q.verify_window(
                [start, periodic, end],
                campaign_id="test318", sequence=1, source_revision=SHA40,
                contract=contract, contract_sha256=contract_sha,
                expected_services=SERVICES,
            )

    def test_restarted_or_oom_container_is_never_accepted(self):
        contract = q.load_contract()
        contract_sha = core.sha256(q.CONTRACT_PATH)
        for field, value in (("restart_count", 1), ("oom_killed", True)):
            bad = observation()
            bad["containers"][0][field] = value
            sample = q.sample_record(
                campaign_id="test318", sequence=1, source_revision=SHA40,
                contract=contract, contract_sha256=contract_sha,
                kind="quiescence-start", window_sample_index=0,
                timestamp_unix_nanos_utc=1, monotonic_elapsed_nanos=0,
                observation=bad,
            )
            sample["ordinal"] = 1
            with self.subTest(field=field):
                with self.assertRaises(q.QuiescenceError):
                    q.validate_sample(
                        sample,
                        campaign_id="test318", sequence=1, source_revision=SHA40,
                        contract=contract, contract_sha256=contract_sha,
                        expected_services=SERVICES,
                    )

    def test_live_collection_uses_compose_service_identity_not_generated_names(self):
        contract = q.load_contract()
        compose = ["docker", "compose", "-f", "/qualified/compose.yaml"]
        ps_rows = [
            {
                "Service": service,
                "Name": f"custom-{service}-9",
                "ID": f"short-{service}",
                "Image": f"image-{service}",
                "State": "running",
                "Health": "healthy",
            }
            for service in SERVICES
        ]
        inspect_rows = [
            {
                "Name": f"/custom-{service}-9",
                "Id": f"full-{service}",
                "RestartCount": 0,
                "State": {"Running": True, "OOMKilled": False, "Pid": 2000 + index},
            }
            for index, service in enumerate(SERVICES)
        ]
        compose_config = {
            "services": {
                service: (
                    {
                        "environment": {
                            "POSTGRES_DB": service.removesuffix("-db"),
                            "POSTGRES_USER": service.removesuffix("-db"),
                        }
                    }
                    if service.endswith("-db")
                    else {"environment": {}}
                )
                for service in SERVICES
            }
        }
        pg = json.dumps({
            "total_connections": 2,
            "active_connections": 1,
            "database_size_bytes": 8192,
            "oxide_batch_tables": [
                {
                    "schema": "oxide_batch",
                    "table": "job_execution",
                    "estimated_rows": 3,
                    "total_bytes": 16384,
                }
            ],
        })

        def runner(command, _cwd):
            if command == [*compose, "ps", "--format", "json"]:
                return json.dumps(ps_rows)
            if command[:2] == ["docker", "inspect"]:
                return json.dumps(inspect_rows)
            if command == [*compose, "config", "--format", "json"]:
                return json.dumps(compose_config)
            if "psql" in command:
                return pg
            raise AssertionError(command)

        disk = lambda _path: SimpleNamespace(total=1000, used=200, free=800)
        observed = q.collect_observation(
            campaign_dir=Path("/evidence"),
            data_path=Path("/data"),
            expected_services=SERVICES,
            contract=contract,
            compose=compose,
            repo_root=Path("/repo"),
            runner=runner,
            meminfo_reader=lambda: {
                "MemTotal": 12 * 1024**3,
                "MemAvailable": 8 * 1024**3,
                "SwapTotal": 2 * 1024**3,
                "SwapFree": 2 * 1024**3,
            },
            load_reader=lambda: (0.1, 0.2, 0.3),
            disk_reader=disk,
            process_probe=lambda pid: {
                "rss_bytes": pid,
                "pss_bytes": pid,
                "thread_count": 1,
                "fd_count": 2,
                "status_unavailable_reason": None,
                "smaps_unavailable_reason": None,
                "fd_unavailable_reason": None,
            },
            cgroup_probe=lambda pid: {
                "memory_current_bytes": pid,
                "memory_peak_bytes": pid + 1,
                "unavailable_reason": None,
            },
        )
        self.assertEqual(
            [item["service"] for item in observed["containers"]],
            SERVICES,
        )
        self.assertEqual(len(observed["postgres"]), 3)
        self.assertTrue(all(item["restart_count"] == 0 for item in observed["containers"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
