#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-spring-enforcer-execution.py")
SPEC = importlib.util.spec_from_file_location("validate_spring_enforcer_execution", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def reviewed_pom(
    *,
    phase: str = "validate",
    goal: str = "enforce",
    properties: str = "",
    plugin_configuration: str = "",
    execution_configuration_prefix: str = "",
    execution_attribute: str = "",
) -> ET.Element:
    return ET.fromstring(
        '<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0">'
        f"{properties}"
        "<build><plugins><plugin>"
        "<groupId>org.apache.maven.plugins</groupId>"
        "<artifactId>maven-enforcer-plugin</artifactId><version>3.6.3</version>"
        f"{plugin_configuration}"
        "<executions><execution"
        f"{execution_attribute}>"
        "<id>enforce-java-build-contract</id>"
        f"<phase>{phase}</phase><goals><goal>{goal}</goal></goals>"
        "<configuration>"
        f"{execution_configuration_prefix}"
        "<rules>"
        "<requireJavaVersion><version>[25,26)</version></requireJavaVersion>"
        "<requireMavenVersion><version>[3.9,4.0)</version></requireMavenVersion>"
        "<requireReleaseDeps><onlyWhenRelease>true</onlyWhenRelease></requireReleaseDeps>"
        "<dependencyConvergence><excludes><exclude>org.jspecify:jspecify</exclude></excludes></dependencyConvergence>"
        "<requireUpperBoundDeps><includes><include>org.jspecify:jspecify</include></includes></requireUpperBoundDeps>"
        "</rules></configuration></execution></executions>"
        "</plugin></plugins></build></project>"
    )


class EnforcerExecutionTests(unittest.TestCase):
    def test_accepts_exact_active_execution(self) -> None:
        validator.validate_enforcer_execution(reviewed_pom())

    def test_rejects_disabled_phase(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "validate phase"):
            validator.validate_enforcer_execution(reviewed_pom(phase="none"))

    def test_rejects_wrong_goal(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "goals must be exactly"):
            validator.validate_enforcer_execution(reviewed_pom(goal="help"))

    def test_rejects_child_property_override(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "properties are forbidden"):
            validator.validate_enforcer_execution(
                reviewed_pom(properties="<properties><enforcer.skip>true</enforcer.skip></properties>")
            )

    def test_rejects_plugin_level_skip(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "enforcer plugin shape"):
            validator.validate_enforcer_execution(
                reviewed_pom(plugin_configuration="<configuration><skip>true</skip></configuration>")
            )

    def test_rejects_execution_level_skip(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "enforcer configuration shape"):
            validator.validate_enforcer_execution(
                reviewed_pom(execution_configuration_prefix="<skip>true</skip>")
            )

    def test_rejects_maven_merge_attributes(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "attributes are forbidden"):
            validator.validate_enforcer_execution(
                reviewed_pom(execution_attribute=' combine.self="override"')
            )


if __name__ == "__main__":
    unittest.main()
