#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("run-supply-chain-authority.py")
SPEC = importlib.util.spec_from_file_location("run_supply_chain_authority", MODULE_PATH)
assert SPEC and SPEC.loader
authority = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(authority)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class DiscoveryContractTests(unittest.TestCase):
    def test_parses_non_empty_unique_names(self) -> None:
        payload = json.dumps({"include": [{"name": "alpha"}, {"name": "beta"}]})
        self.assertEqual(authority.parse_discovery_json(payload), ["alpha", "beta"])

    def test_rejects_empty_or_malformed_projection(self) -> None:
        for payload in (
            "{}",
            json.dumps({"include": []}),
            json.dumps({"include": [{"path": "alpha"}]}),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(authority.SupplyChainAuthorityError):
                    authority.parse_discovery_json(payload)

    def test_rejects_duplicate_names(self) -> None:
        payload = json.dumps({"include": [{"name": "alpha"}, {"name": "alpha"}]})
        with self.assertRaisesRegex(authority.SupplyChainAuthorityError, "duplicate"):
            authority.parse_discovery_json(payload)

    def test_rejects_discovery_registry_mismatch(self) -> None:
        with self.assertRaisesRegex(authority.SupplyChainAuthorityError, "mismatch"):
            authority.verify_discovery_names(["alpha", "beta"], ["alpha", "gamma"])

    def test_real_repository_projection_matches_canonical_registry(self) -> None:
        canonical = authority.canonical_real_workloads(REPO_ROOT)
        payload = json.dumps({"include": [{"name": name} for name in canonical]})
        self.assertEqual(authority.verify_discovery_contract(REPO_ROOT, payload), canonical)
        self.assertNotIn("fixture-heterogeneous", canonical)


class RunAllTests(unittest.TestCase):
    def test_runs_every_workload_and_accumulates_failures(self) -> None:
        calls: list[str] = []

        def scan(name: str) -> int:
            calls.append(name)
            return 7 if name == "beta" else 0

        failures = authority.run_all(["alpha", "beta", "gamma"], scan)
        self.assertEqual(calls, ["alpha", "beta", "gamma"])
        self.assertEqual(failures, ["beta: validator exited with code 7"])

    def test_converts_validator_errors_and_continues(self) -> None:
        calls: list[str] = []

        def scan(name: str) -> int:
            calls.append(name)
            if name == "alpha":
                raise authority.sc.SupplyChainError("broken registry subject")
            return 0

        failures = authority.run_all(["alpha", "beta"], scan)
        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(failures, ["alpha: broken registry subject"])


if __name__ == "__main__":
    unittest.main()
