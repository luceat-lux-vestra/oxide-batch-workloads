#!/usr/bin/env python3
"""Fail-closed JBeret Maven convergence exception policy for campaign #86."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

JBERET_POM = Path(__file__).resolve().parents[1] / "jberet" / "pom.xml"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
EXPECTED_COORDINATES = [
    "jakarta.el:jakarta.el-api",
    "jakarta.inject:jakarta.inject-api",
    "jakarta.enterprise:jakarta.enterprise.cdi-api",
    "org.jboss.logging:jboss-logging",
    "jakarta.interceptor:jakarta.interceptor-api",
    "jakarta.annotation:jakarta.annotation-api",
]


class ValidationError(ValueError):
    pass


def text(node: ET.Element | None, field: str) -> str:
    if node is None or node.text is None or not node.text.strip():
        raise ValidationError(f"missing {field}")
    return node.text.strip()


def parse(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValidationError(f"invalid XML in {path}: {exc}") from exc


def validate_jberet_enforcer(root: ET.Element) -> None:
    plugins = root.findall("m:build/m:plugins/m:plugin", NS)
    enforcers = [
        plugin
        for plugin in plugins
        if text(plugin.find("m:artifactId", NS), "plugin artifactId") == "maven-enforcer-plugin"
    ]
    if len(enforcers) != 1:
        raise ValidationError(f"JBeret must declare exactly one local maven-enforcer-plugin, got {len(enforcers)}")

    plugin = enforcers[0]
    expected_plugin_children = {"groupId", "artifactId", "version", "executions"}
    actual_plugin_children = {child.tag.rsplit("}", 1)[-1] for child in plugin}
    if actual_plugin_children != expected_plugin_children:
        raise ValidationError(
            f"JBeret enforcer plugin controls drifted: expected {sorted(expected_plugin_children)!r}, "
            f"got {sorted(actual_plugin_children)!r}"
        )
    if text(plugin.find("m:groupId", NS), "enforcer groupId") != "org.apache.maven.plugins":
        raise ValidationError("JBeret enforcer groupId must be org.apache.maven.plugins")
    if text(plugin.find("m:version", NS), "enforcer version") != "3.6.3":
        raise ValidationError("JBeret enforcer version must be exactly 3.6.3")

    executions = plugin.findall("m:executions/m:execution", NS)
    if len(executions) != 1:
        raise ValidationError(f"JBeret enforcer must contain exactly one execution, got {len(executions)}")
    execution = executions[0]
    if text(execution.find("m:id", NS), "enforcer execution id") != "enforce-java-build-contract":
        raise ValidationError("JBeret enforcer must override exactly enforce-java-build-contract")
    execution_children = {child.tag.rsplit("}", 1)[-1] for child in execution}
    if execution_children != {"id", "configuration"}:
        raise ValidationError(
            f"JBeret enforcer execution may contain only id/configuration, got {sorted(execution_children)!r}"
        )

    configuration = execution.find("m:configuration", NS)
    if configuration is None:
        raise ValidationError("JBeret enforcer is missing configuration")
    configuration_children = [child.tag.rsplit("}", 1)[-1] for child in configuration]
    if configuration_children != ["rules"]:
        raise ValidationError(
            f"JBeret enforcer configuration may contain only rules, got {configuration_children!r}"
        )
    rules = configuration.find("m:rules", NS)
    if rules is None:
        raise ValidationError("JBeret enforcer is missing rules")
    rule_names = [child.tag.rsplit("}", 1)[-1] for child in rules]
    if rule_names != ["dependencyConvergence", "requireUpperBoundDeps"]:
        raise ValidationError(
            "JBeret enforcer rules must be exactly dependencyConvergence then requireUpperBoundDeps; "
            f"got {rule_names!r}"
        )

    excludes = [
        text(node, "dependency convergence exclude")
        for node in rules.findall("m:dependencyConvergence/m:excludes/m:exclude", NS)
    ]
    includes = [
        text(node, "upper-bound include")
        for node in rules.findall("m:requireUpperBoundDeps/m:includes/m:include", NS)
    ]
    if excludes != EXPECTED_COORDINATES:
        raise ValidationError(
            f"JBeret convergence exclusions must be exactly {EXPECTED_COORDINATES!r}, got {excludes!r}"
        )
    if includes != EXPECTED_COORDINATES:
        raise ValidationError(
            f"JBeret upper-bound includes must exactly mirror exclusions {EXPECTED_COORDINATES!r}, got {includes!r}"
        )

    convergence = rules.find("m:dependencyConvergence", NS)
    upper = rules.find("m:requireUpperBoundDeps", NS)
    if convergence is None or upper is None:
        raise ValidationError("JBeret enforcer rule nodes are incomplete")
    if [child.tag.rsplit("}", 1)[-1] for child in convergence] != ["excludes"]:
        raise ValidationError("JBeret dependencyConvergence may contain only the reviewed excludes list")
    if [child.tag.rsplit("}", 1)[-1] for child in upper] != ["includes"]:
        raise ValidationError("JBeret requireUpperBoundDeps may contain only the reviewed includes list")


def main() -> None:
    try:
        validate_jberet_enforcer(parse(JBERET_POM))
    except ValidationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("JBeret scoped convergence/upper-bound policy validation passed")


if __name__ == "__main__":
    main()
