#!/usr/bin/env python3
"""Fail-closed Java 25 compiler/runtime baseline contract for campaign #79."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

JAVA_ROOT = Path(__file__).resolve().parents[1]
SPRING_ROOT = JAVA_ROOT / "spring-batch"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
EXPECTED_RELEASE = "25"
EXPECTED_RANGE = "[25,26)"
ENFORCER_EXECUTION_ID = "enforce-java-build-contract"


class ValidationError(ValueError):
    pass


def text(element: ET.Element | None, field: str) -> str:
    if element is None or element.text is None or not element.text.strip():
        raise ValidationError(f"missing {field}")
    return element.text.strip()


def single_plugin(root: ET.Element, artifact_id: str) -> ET.Element:
    matches = [
        plugin
        for plugin in root.findall("m:build/m:plugins/m:plugin", NS)
        if text(plugin.find("m:artifactId", NS), "plugin artifactId") == artifact_id
    ]
    if len(matches) != 1:
        raise ValidationError(f"expected exactly one {artifact_id} plugin, got {len(matches)}")
    return matches[0]


def enforcer_java_range(root: ET.Element) -> str:
    enforcer = single_plugin(root, "maven-enforcer-plugin")
    executions = [
        execution
        for execution in enforcer.findall("m:executions/m:execution", NS)
        if text(execution.find("m:id", NS), "enforcer execution id") == ENFORCER_EXECUTION_ID
    ]
    if len(executions) != 1:
        raise ValidationError(
            f"expected exactly one {ENFORCER_EXECUTION_ID} enforcer execution, got {len(executions)}"
        )
    return text(
        executions[0].find("m:configuration/m:rules/m:requireJavaVersion/m:version", NS),
        "enforcer requireJavaVersion range",
    )


def validate_root(root: ET.Element) -> None:
    release_property = text(
        root.find("m:properties/m:maven.compiler.release", NS),
        "maven.compiler.release",
    )
    if release_property != EXPECTED_RELEASE:
        raise ValidationError(
            f"root maven.compiler.release must remain {EXPECTED_RELEASE}, got {release_property!r}"
        )

    compiler = single_plugin(root, "maven-compiler-plugin")
    compiler_release = text(compiler.find("m:configuration/m:release", NS), "compiler plugin release")
    if compiler_release != EXPECTED_RELEASE:
        raise ValidationError(
            f"maven-compiler-plugin release must remain {EXPECTED_RELEASE}, got {compiler_release!r}"
        )

    java_range = enforcer_java_range(root)
    if java_range != EXPECTED_RANGE:
        raise ValidationError(
            f"root Maven Enforcer Java range must remain {EXPECTED_RANGE}, got {java_range!r}"
        )


def validate_spring(root: ET.Element) -> None:
    java_range = enforcer_java_range(root)
    if java_range != EXPECTED_RANGE:
        raise ValidationError(
            f"Spring Maven Enforcer Java range must remain {EXPECTED_RANGE}, got {java_range!r}"
        )


def parse(path: Path) -> ET.Element:
    if not path.is_file() or path.is_symlink():
        raise ValidationError(f"expected regular Maven manifest: {path}")
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValidationError(f"invalid XML in {path}: {exc}") from exc


def main() -> int:
    try:
        validate_root(parse(JAVA_ROOT / "pom.xml"))
        validate_spring(parse(SPRING_ROOT / "pom.xml"))
    except ValidationError as exc:
        print(f"java25 baseline validation failed: {exc}", file=sys.stderr)
        return 1

    print("java25 baseline validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
