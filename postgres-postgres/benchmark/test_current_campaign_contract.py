#!/usr/bin/env python3
"""Prevent current allocator/profile evidence from drifting back to #266 / 0.6.0."""

from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
WORKLOAD_ROOT = HERE.parent
REPOSITORY_ROOT = WORKLOAD_ROOT.parent

SPEC = importlib.util.spec_from_file_location(
    "profile_manifest",
    HERE / "profile_manifest.py",
)
assert SPEC and SPEC.loader
profile_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile_manifest)

CURRENT_PROFILE_WORKFLOWS = (
    "profile-postgres-postgres-cpu.yml",
    "profile-postgres-postgres-heap.yml",
    "profile-postgres-postgres-perfetto-heap.yml",
    "profile-postgres-postgres-timeline.yml",
    "profile-postgres-postgres-allocator-native.yml",
    "profile-postgres-postgres-os-process.yml",
)

CURRENT_CAPABILITY_WORKFLOWS = (
    "probe-perfetto-heap-capability.yml",
)

CURRENT_PRODUCERS = (
    "profile_smoke.py",
    "allocator_benchmark.py",
    "cpu_profile.py",
    "cpu_profile_gperftools.py",
    "heap_profile.py",
    "perfetto_heap_profile.py",
    "timeline_profile.py",
    "allocator_telemetry_profile.py",
    "os_process_profile.py",
)

STALE_CAMPAIGN_ASSERTION = re.compile(r'\["campaign"\]\s*==\s*266')
STALE_CAMPAIGN_LITERAL = re.compile(r'"campaign"\s*:\s*266\b')


class CurrentCampaignContractTests(unittest.TestCase):
    def test_manifest_owner_is_current_release_campaign(self) -> None:
        self.assertEqual(profile_manifest.CAMPAIGN, 323)
        self.assertEqual(profile_manifest.OXIDE_BATCH_VERSION, "0.7.0")
        self.assertEqual(
            profile_manifest.CAMPAIGN_OXIDE_BATCH_VERSIONS,
            {266: "0.6.0", 323: "0.7.0"},
        )

    def test_current_producers_emit_registry_owned_campaign(self) -> None:
        for filename in CURRENT_PRODUCERS:
            with self.subTest(filename=filename):
                source = (HERE / filename).read_text(encoding="utf-8")
                self.assertIn('"campaign": profile_manifest.CAMPAIGN', source)

    def test_current_profile_workflows_require_323_and_0_7(self) -> None:
        workflow_root = REPOSITORY_ROOT / ".github" / "workflows"
        for filename in CURRENT_PROFILE_WORKFLOWS:
            with self.subTest(filename=filename):
                source = (workflow_root / filename).read_text(encoding="utf-8")
                self.assertNotRegex(source, STALE_CAMPAIGN_ASSERTION)
                self.assertNotRegex(source, STALE_CAMPAIGN_LITERAL)
                self.assertNotIn('"0.6.0"', source)
                self.assertIn("323", source)
                self.assertIn('"0.7.0"', source)

    def test_current_capability_workflows_do_not_emit_campaign_266(self) -> None:
        workflow_root = REPOSITORY_ROOT / ".github" / "workflows"
        for filename in CURRENT_CAPABILITY_WORKFLOWS:
            with self.subTest(filename=filename):
                source = (workflow_root / filename).read_text(encoding="utf-8")
                self.assertNotRegex(source, STALE_CAMPAIGN_ASSERTION)
                self.assertNotRegex(source, STALE_CAMPAIGN_LITERAL)
                self.assertIn("323", source)


if __name__ == "__main__":
    unittest.main()
