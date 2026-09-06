#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-jberet-enforcer.py")
SPEC = importlib.util.spec_from_file_location("validate_jberet_enforcer", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)
ORIGINAL = validator.JBERET_POM.read_text(encoding="utf-8")


def root(text: str = ORIGINAL) -> ET.Element:
    return ET.fromstring(text)


class JBeretEnforcerPolicyTests(unittest.TestCase):
    def test_current_policy_passes(self) -> None:
        validator.validate_jberet_enforcer(root())

    def test_extra_exclusion_fails(self) -> None:
        mutated = ORIGINAL.replace(
            "</excludes>", "<exclude>example:shadow</exclude></excludes>", 1
        )
        with self.assertRaisesRegex(validator.ValidationError, "exclusions must be exactly"):
            validator.validate_jberet_enforcer(root(mutated))

    def test_missing_exclusion_fails(self) -> None:
        mutated = ORIGINAL.replace(
            "<exclude>jakarta.el:jakarta.el-api</exclude>", "", 1
        )
        with self.assertRaisesRegex(validator.ValidationError, "exclusions must be exactly"):
            validator.validate_jberet_enforcer(root(mutated))

    def test_upper_bound_must_mirror_exclusions(self) -> None:
        mutated = ORIGINAL.replace(
            "<include>jakarta.el:jakarta.el-api</include>",
            "<include>example:shadow</include>",
            1,
        )
        with self.assertRaisesRegex(validator.ValidationError, "upper-bound includes"):
            validator.validate_jberet_enforcer(root(mutated))

    def test_wrong_execution_id_fails(self) -> None:
        mutated = ORIGINAL.replace(
            "<id>enforce-java-build-contract</id>", "<id>shadow-contract</id>", 1
        )
        with self.assertRaisesRegex(validator.ValidationError, "override exactly"):
            validator.validate_jberet_enforcer(root(mutated))

    def test_additional_execution_fails(self) -> None:
        mutated = ORIGINAL.replace(
            "</executions>",
            "<execution><id>shadow</id><configuration/></execution></executions>",
            1,
        )
        with self.assertRaisesRegex(validator.ValidationError, "exactly one execution"):
            validator.validate_jberet_enforcer(root(mutated))

    def test_unreviewed_rule_fails(self) -> None:
        mutated = ORIGINAL.replace(
            "</rules>", "<banDuplicatePomDependencyVersions/></rules>", 1
        )
        with self.assertRaisesRegex(validator.ValidationError, "rules must be exactly"):
            validator.validate_jberet_enforcer(root(mutated))

    def test_skip_control_fails(self) -> None:
        mutated = ORIGINAL.replace(
            "<configuration>", "<configuration><skip>true</skip>", 1
        )
        with self.assertRaisesRegex(validator.ValidationError, "rules"):
            validator.validate_jberet_enforcer(root(mutated))


if __name__ == "__main__":
    unittest.main()
