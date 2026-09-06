#!/usr/bin/env python3
"""Fail-closed validation for the Spring module's scoped Maven Enforcer override."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

JAVA_ROOT = Path(__file__).resolve().parents[1]
SPRING_POM = JAVA_ROOT / "spring-batch" / "pom.xml"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
EXPECTED_RULES = (
    "requireJavaVersion",
    "requireMavenVersion",
    "requireReleaseDeps",
    "dependencyConvergence",
    "requireUpperBoundDeps",
)


class ValidationError(ValueError):
    pass


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def text(element: ET.Element | None, field: str) -> str:
    if element is None or element.text is None or not element.text.strip():
        raise ValidationError(f"missing {field}")
    return element.text.strip()


def require_child_shape(element: ET.Element, expected: tuple[str, ...], field: str) -> None:
    actual = tuple(local_name(child) for child in element)
    if actual != expected:
        raise ValidationError(f"{field} shape must be exactly {expected!r}, got {actual!r}")


def validate_enforcer_execution(root: ET.Element) -> None:
    if root.find("m:properties", NS) is not None:
        raise ValidationError("spring-batch child POM properties are forbidden; they could disable inherited build policy")

    enforcers = []
    for plugin in root.findall("m:build/m:plugins/m:plugin", NS):
        group = plugin.find("m:groupId", NS)
        group_id = text(group, "plugin groupId") if group is not None else "org.apache.maven.plugins"
        artifact_id = text(plugin.find("m:artifactId", NS), "plugin artifactId")
        if (group_id, artifact_id) == ("org.apache.maven.plugins", "maven-enforcer-plugin"):
            enforcers.append(plugin)
    if len(enforcers) != 1:
        raise ValidationError(f"spring-batch must declare exactly one maven-enforcer-plugin, got {len(enforcers)}")

    plugin = enforcers[0]
    require_child_shape(plugin, ("groupId", "artifactId", "version", "executions"), "enforcer plugin")
    if text(plugin.find("m:version", NS), "enforcer plugin version") != "3.6.3":
        raise ValidationError("spring-batch enforcer plugin must remain exactly 3.6.3")

    executions = plugin.findall("m:executions/m:execution", NS)
    if len(executions) != 1:
        raise ValidationError(f"spring-batch enforcer override must contain exactly one execution, got {len(executions)}")
    execution = executions[0]
    require_child_shape(execution, ("id", "phase", "goals", "configuration"), "enforcer execution")
    if any(element.attrib for element in execution.iter()):
        raise ValidationError("enforcer execution/configuration attributes are forbidden")
    if text(execution.find("m:id", NS), "enforcer execution id") != "enforce-java-build-contract":
        raise ValidationError("spring-batch enforcer execution id must remain enforce-java-build-contract")
    if text(execution.find("m:phase", NS), "enforcer phase") != "validate":
        raise ValidationError("spring-batch enforcer execution must remain bound to the validate phase")
    goals = [text(goal, "enforcer goal") for goal in execution.findall("m:goals/m:goal", NS)]
    if goals != ["enforce"]:
        raise ValidationError(f"spring-batch enforcer goals must be exactly ['enforce'], got {goals!r}")

    configuration = execution.find("m:configuration", NS)
    if configuration is None:
        raise ValidationError("missing spring-batch enforcer configuration")
    require_child_shape(configuration, ("rules",), "enforcer configuration")
    rules = configuration.find("m:rules", NS)
    if rules is None:
        raise ValidationError("missing spring-batch enforcer rules")
    require_child_shape(rules, EXPECTED_RULES, "enforcer rules")

    if text(rules.find("m:requireJavaVersion/m:version", NS), "required Java version") != "[21,22)":
        raise ValidationError("spring-batch enforcer Java range must remain [21,22)")
    if text(rules.find("m:requireMavenVersion/m:version", NS), "required Maven version") != "[3.9,4.0)":
        raise ValidationError("spring-batch enforcer Maven range must remain [3.9,4.0)")
    if text(rules.find("m:requireReleaseDeps/m:onlyWhenRelease", NS), "requireReleaseDeps onlyWhenRelease") != "true":
        raise ValidationError("spring-batch requireReleaseDeps.onlyWhenRelease must remain true")

    excludes = [
        text(item, "dependencyConvergence exclude")
        for item in rules.findall("m:dependencyConvergence/m:excludes/m:exclude", NS)
    ]
    if excludes != ["org.jspecify:jspecify"]:
        raise ValidationError(f"dependencyConvergence exception must be exactly org.jspecify:jspecify, got {excludes!r}")
    includes = [
        text(item, "requireUpperBoundDeps include")
        for item in rules.findall("m:requireUpperBoundDeps/m:includes/m:include", NS)
    ]
    if includes != ["org.jspecify:jspecify"]:
        raise ValidationError(f"requireUpperBoundDeps target must be exactly org.jspecify:jspecify, got {includes!r}")


def main() -> None:
    try:
        root = ET.parse(SPRING_POM).getroot()
        validate_enforcer_execution(root)
    except (ET.ParseError, OSError, ValidationError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Spring Maven Enforcer execution policy validation passed")


if __name__ == "__main__":
    main()
