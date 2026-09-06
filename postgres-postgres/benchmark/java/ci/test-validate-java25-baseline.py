#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-java25-baseline.py")
SPEC = importlib.util.spec_from_file_location("validate_java25_baseline", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def pom(body: str) -> ET.Element:
    return ET.fromstring(
        '<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0">'
        + body
        + "</project>"
    )


def root_pom(
    *,
    property_release: str = "25",
    compiler_release: str = "25",
    java_range: str = "[25,26)",
    duplicate_enforcer: bool = False,
) -> ET.Element:
    enforcer = (
        "<plugin><groupId>org.apache.maven.plugins</groupId>"
        "<artifactId>maven-enforcer-plugin</artifactId><version>3.6.3</version>"
        "<executions><execution><id>enforce-java-build-contract</id>"
        "<configuration><rules><requireJavaVersion><version>"
        f"{java_range}"
        "</version></requireJavaVersion></rules></configuration>"
        "</execution></executions></plugin>"
    )
    return pom(
        "<properties><maven.compiler.release>"
        f"{property_release}"
        "</maven.compiler.release></properties>"
        "<build><plugins>"
        "<plugin><groupId>org.apache.maven.plugins</groupId>"
        "<artifactId>maven-compiler-plugin</artifactId><version>3.16.0</version>"
        "<configuration><release>"
        f"{compiler_release}"
        "</release></configuration></plugin>"
        f"{enforcer}"
        f"{enforcer if duplicate_enforcer else ''}"
        "</plugins></build>"
    )


def spring_pom(*, java_range: str = "[25,26)") -> ET.Element:
    return pom(
        "<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>"
        "<artifactId>maven-enforcer-plugin</artifactId><version>3.6.3</version>"
        "<executions><execution><id>enforce-java-build-contract</id>"
        "<configuration><rules><requireJavaVersion><version>"
        f"{java_range}"
        "</version></requireJavaVersion></rules></configuration>"
        "</execution></executions></plugin></plugins></build>"
    )


class Java25BaselineTests(unittest.TestCase):
    def test_accepts_exact_root_and_spring_contracts(self) -> None:
        validator.validate_root(root_pom())
        validator.validate_spring(spring_pom())

    def test_rejects_root_property_release_drift(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "maven.compiler.release"):
            validator.validate_root(root_pom(property_release="21"))

    def test_rejects_compiler_plugin_release_drift(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "compiler-plugin release"):
            validator.validate_root(root_pom(compiler_release="21"))

    def test_rejects_root_enforcer_range_drift(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "root Maven Enforcer"):
            validator.validate_root(root_pom(java_range="[21,22)"))

    def test_rejects_spring_enforcer_range_drift(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "Spring Maven Enforcer"):
            validator.validate_spring(spring_pom(java_range="[21,22)"))

    def test_rejects_duplicate_enforcer_plugin(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "exactly one maven-enforcer-plugin"):
            validator.validate_root(root_pom(duplicate_enforcer=True))


if __name__ == "__main__":
    unittest.main()
