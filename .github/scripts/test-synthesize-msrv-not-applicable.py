#!/usr/bin/env python3

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("synthesize-msrv-not-applicable.py")
SPEC = importlib.util.spec_from_file_location("synthesize_msrv_not_applicable", MODULE_PATH)
assert SPEC and SPEC.loader
synth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(synth)


def declared(name: str, version: str = "1.90") -> dict:
    return {"name": name, "path": name, "msrv": {"declared": True, "version": version, "policy_reason": None}}


def undeclared(name: str, reason: str = "no MSRV policy") -> dict:
    return {"name": name, "path": name, "msrv": {"declared": False, "version": None, "policy_reason": reason}}


class SynthesisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.results = Path(self.tempdir.name) / "results"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def payload(self, entries: list[dict]) -> str:
        return json.dumps({"include": entries})

    def test_synthesizes_every_undeclared_entry_only(self) -> None:
        paths = synth.synthesize(
            self.payload([declared("alpha"), undeclared("fixture-one"), undeclared("fixture-two")]),
            self.results,
        )
        self.assertEqual(
            sorted(path.parent.name for path in paths),
            ["synthetic-msrv-fixture-one", "synthetic-msrv-fixture-two"],
        )
        results, errors = synth.aggregate.load_results(self.results, "msrv")
        self.assertEqual(errors, [])
        by_name = {result.workload: result for result in results}
        self.assertEqual(set(by_name), {"fixture-one", "fixture-two"})
        self.assertTrue(all(result.outcome == "not-applicable" for result in by_name.values()))

    def test_declared_entry_cannot_be_synthesized_as_not_applicable(self) -> None:
        with self.assertRaisesRegex(synth.SynthesisError, "refusing to synthesize"):
            synth.build_not_applicable_result(declared("alpha"))

    def test_rejects_duplicate_names(self) -> None:
        with self.assertRaisesRegex(synth.SynthesisError, "duplicate"):
            synth.parse_entries(self.payload([declared("alpha"), undeclared("alpha")]))

    def test_rejects_missing_policy_reason(self) -> None:
        bad = undeclared("fixture")
        bad["msrv"]["policy_reason"] = ""
        with self.assertRaisesRegex(synth.SynthesisError, "non-empty policy reason"):
            synth.parse_entries(self.payload([declared("alpha"), bad]))

    def test_rejects_malformed_discovery(self) -> None:
        for payload in ("{bad json", "{}", json.dumps({"include": []}), json.dumps({"include": ["alpha"]})):
            with self.subTest(payload=payload):
                with self.assertRaises(synth.SynthesisError):
                    synth.parse_entries(payload)

    def test_rejects_collision_with_downloaded_result(self) -> None:
        existing = self.results / "result-msrv-fixture" / "result.json"
        existing.parent.mkdir(parents=True)
        existing.write_text(
            json.dumps(
                {
                    "workload": "fixture",
                    "stage": "msrv",
                    "status": "success",
                    "outcome": "not-applicable",
                    "reason": "existing",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(synth.SynthesisError, "collide"):
            synth.synthesize(self.payload([declared("alpha"), undeclared("fixture")]), self.results)

    def test_rejects_malformed_existing_result(self) -> None:
        bad = self.results / "bad" / "result.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{bad json", encoding="utf-8")
        with self.assertRaisesRegex(synth.SynthesisError, "malformed"):
            synth.synthesize(self.payload([declared("alpha"), undeclared("fixture")]), self.results)


if __name__ == "__main__":
    unittest.main()
