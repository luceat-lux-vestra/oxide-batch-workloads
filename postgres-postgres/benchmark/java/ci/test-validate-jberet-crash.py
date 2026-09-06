#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-jberet-crash.py")
spec = importlib.util.spec_from_file_location("validate_jberet_crash", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CrashBoundaryValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main_source = module.MAIN_SOURCE.read_text(encoding="utf-8")
        cls.main_jsl = module.MAIN_JSL.read_text(encoding="utf-8")
        cls.crash_source = module.CRASH_SOURCE.read_text(encoding="utf-8")
        cls.crash_jsl = module.CRASH_JSL.read_text(encoding="utf-8")

    def test_current_surfaces_pass(self) -> None:
        module.validate_production_boundary(self.main_source, self.main_jsl)
        module.validate_crash_source(self.crash_source)
        module.validate_crash_jsl(self.crash_jsl)

    def test_production_instrumentation_leak_fails(self) -> None:
        with self.assertRaises(module.ValidationError):
            module.validate_production_boundary(self.main_source + "\npauseAtChunk\n", self.main_jsl)

    def test_missing_public_restart_fails(self) -> None:
        mutated = self.crash_source.replace("operator.restart(executionId, restartParameters)", "operator.restart(executionId, null)")
        with self.assertRaises(module.ValidationError):
            module.validate_crash_source(mutated)

    def test_missing_public_metrics_fails(self) -> None:
        mutated = self.crash_source.replace("step.getMetrics()", "step.toString()")
        with self.assertRaises(module.ValidationError):
            module.validate_crash_source(mutated)

    def test_upsert_repair_fails(self) -> None:
        with self.assertRaises(module.ValidationError):
            module.validate_crash_source(self.crash_source + "\nON CONFLICT DO NOTHING\n")

    def test_repository_dml_fails(self) -> None:
        with self.assertRaises(module.ValidationError):
            module.validate_crash_source(self.crash_source + "\nUPDATE jberet.step_execution SET writecount=0;\n")

    def test_writer_substitution_fails(self) -> None:
        mutated = self.crash_jsl.replace(
            "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresWriter",
            "io.example.RepairingWriter",
        )
        with self.assertRaises(module.ValidationError):
            module.validate_crash_jsl(mutated)

    def test_listener_removal_fails(self) -> None:
        mutated = self.crash_jsl.replace("<listeners>", "<listeners-disabled>").replace("</listeners>", "</listeners-disabled>")
        with self.assertRaises(module.ValidationError):
            module.validate_crash_jsl(mutated)


if __name__ == "__main__":
    unittest.main()
