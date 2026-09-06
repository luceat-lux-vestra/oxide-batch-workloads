#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-poms.py")
SPEC = importlib.util.spec_from_file_location("validate_poms", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def spring_pom(
    *,
    excludes: tuple[str, ...] = ("org.jspecify:jspecify",),
    upper_bound_includes: tuple[str, ...] = ("org.jspecify:jspecify",),
    include_enforcer: bool = True,
) -> ET.Element:
    enforcer = ""
    if include_enforcer:
        excludes_xml = "".join(f"<exclude>{value}</exclude>" for value in excludes)
        includes_xml = "".join(f"<include>{value}</include>" for value in upper_bound_includes)
        enforcer = (
            "<build><plugins><plugin>"
            "<groupId>org.apache.maven.plugins</groupId>"
            "<artifactId>maven-enforcer-plugin</artifactId><version>3.6.3</version>"
            "<executions><execution><id>enforce-java-build-contract</id>"
            "<phase>validate</phase><goals><goal>enforce</goal></goals>"
            "<configuration><rules>"
            f"<dependencyConvergence><excludes>{excludes_xml}</excludes></dependencyConvergence>"
            f"<requireUpperBoundDeps><includes>{includes_xml}</includes></requireUpperBoundDeps>"
            "</rules></configuration></execution></executions>"
            "</plugin></plugins></build>"
        )
    xml = (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<parent><groupId>io.oxidebatch.validation</groupId>"
        "<artifactId>postgres-postgres-java-benchmark</artifactId><version>0.1.0</version>"
        "<relativePath>../pom.xml</relativePath></parent>"
        "<artifactId>spring-batch</artifactId><packaging>jar</packaging>"
        "<dependencies>"
        "<dependency><groupId>org.springframework.batch</groupId>"
        "<artifactId>spring-batch-core</artifactId><version>6.0.5</version></dependency>"
        "<dependency><groupId>org.postgresql</groupId><artifactId>postgresql</artifactId>"
        "<version>42.7.13</version></dependency>"
        "</dependencies>"
        f"{enforcer}</project>"
    )
    return ET.fromstring(xml)


class SpringConvergencePolicyTests(unittest.TestCase):
    def test_accepts_only_reviewed_jspecify_exception(self) -> None:
        validator.validate_spring(spring_pom())

    def test_rejects_missing_scoped_enforcer_override(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "explicitly scope"):
            validator.validate_spring(spring_pom(include_enforcer=False))

    def test_rejects_additional_convergence_exception(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "dependencyConvergence exception"):
            validator.validate_spring(
                spring_pom(excludes=("org.jspecify:jspecify", "example:shadow"))
            )

    def test_rejects_different_upper_bound_target(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "requireUpperBoundDeps"):
            validator.validate_spring(
                spring_pom(upper_bound_includes=("example:shadow",))
            )


if __name__ == "__main__":
    unittest.main()
