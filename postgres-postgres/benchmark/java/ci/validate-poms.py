#!/usr/bin/env python3
"""Fail-closed Maven/JDBC boundary checks for campaign #79's Java controls."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

JAVA_ROOT = Path(__file__).resolve().parents[1]
WORKLOAD_ROOT = JAVA_ROOT.parent.parent
RAW_ROOT = JAVA_ROOT / "raw-jdbc"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
DYNAMIC = re.compile(r"(?:SNAPSHOT|LATEST|RELEASE|[\[\]\(\),])", re.IGNORECASE)
EXPECTED_MANIFESTS = ("benchmark/java/pom.xml", "benchmark/java/raw-jdbc/pom.xml")
EXPECTED_RAW_SOURCES = (
    "src/main/java/io/oxidebatch/workloads/postgres/benchmark/rawjdbc/RawJdbcMain.java",
)


class ValidationError(ValueError):
    pass


def text(element: ET.Element | None, field: str) -> str:
    if element is None or element.text is None or not element.text.strip():
        raise ValidationError(f"missing {field}")
    return element.text.strip()


def exact_version(value: str, field: str) -> None:
    if "${" in value or DYNAMIC.search(value):
        raise ValidationError(f"{field} must use an exact literal release version, got {value!r}")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z_-]+)+", value):
        raise ValidationError(f"{field} is not an accepted exact version literal: {value!r}")


def validate_manifest_inventory() -> None:
    manifests = tuple(
        sorted(str(path.relative_to(WORKLOAD_ROOT)) for path in WORKLOAD_ROOT.rglob("pom.xml"))
    )
    if manifests != EXPECTED_MANIFESTS:
        raise ValidationError(
            "PR1 Maven manifest inventory must be exactly "
            f"{EXPECTED_MANIFESTS!r}, got {manifests!r}"
        )


def validate_source_inventory() -> None:
    source_root = RAW_ROOT / "src/main/java"
    sources = tuple(
        sorted(str(path.relative_to(RAW_ROOT)) for path in source_root.rglob("*.java"))
    )
    if sources != EXPECTED_RAW_SOURCES:
        raise ValidationError(
            "PR1 raw-JDBC source inventory must be exactly "
            f"{EXPECTED_RAW_SOURCES!r}, got {sources!r}"
        )


def parse(path: Path) -> ET.Element:
    if path.is_symlink():
        raise ValidationError(f"Maven project file must not be a symlink: {path.relative_to(JAVA_ROOT)}")
    if not path.is_file():
        raise ValidationError(f"missing Maven project file: {path.relative_to(JAVA_ROOT)}")
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValidationError(f"invalid XML in {path.relative_to(JAVA_ROOT)}: {exc}") from exc


def validate_common(path: Path, root: ET.Element) -> None:
    if root.find("m:profiles", NS) is not None:
        raise ValidationError(f"Maven profiles are forbidden in PR1: {path.relative_to(JAVA_ROOT)}")
    if root.find(".//m:repositories", NS) is not None:
        raise ValidationError(f"custom Maven repositories are forbidden: {path.relative_to(JAVA_ROOT)}")
    if root.find(".//m:pluginRepositories", NS) is not None:
        raise ValidationError(
            f"custom Maven pluginRepositories are forbidden: {path.relative_to(JAVA_ROOT)}"
        )
    if root.find(".//m:build/m:extensions/m:extension", NS) is not None:
        raise ValidationError(f"Maven build extensions are forbidden: {path.relative_to(JAVA_ROOT)}")

    project_version = root.find("m:version", NS)
    if project_version is not None:
        exact_version(text(project_version, "project version"), f"{path.name} project version")

    parent = root.find("m:parent", NS)
    if parent is not None:
        exact_version(
            text(parent.find("m:version", NS), "parent version"),
            f"{path.name} parent version",
        )

    for dependency in root.findall(".//m:dependencies/m:dependency", NS):
        group = text(dependency.find("m:groupId", NS), "dependency groupId")
        artifact = text(dependency.find("m:artifactId", NS), "dependency artifactId")
        version = text(dependency.find("m:version", NS), f"dependency version for {group}:{artifact}")
        exact_version(version, f"dependency {group}:{artifact}")

    for plugin in root.findall(".//m:build/m:plugins/m:plugin", NS):
        group = plugin.find("m:groupId", NS)
        group_value = text(group, "plugin groupId") if group is not None else "org.apache.maven.plugins"
        artifact = text(plugin.find("m:artifactId", NS), "plugin artifactId")
        version = text(plugin.find("m:version", NS), f"plugin version for {group_value}:{artifact}")
        exact_version(version, f"plugin {group_value}:{artifact}")


def validate_parent(root: ET.Element) -> None:
    if root.find("m:parent", NS) is not None:
        raise ValidationError("Java reactor root must not inherit an external Maven parent")
    if root.find("m:dependencies", NS) is not None or root.find("m:dependencyManagement", NS) is not None:
        raise ValidationError("Java reactor root must not contribute inherited runtime dependencies")

    coordinates = (
        text(root.find("m:groupId", NS), "root groupId"),
        text(root.find("m:artifactId", NS), "root artifactId"),
        text(root.find("m:version", NS), "root version"),
        text(root.find("m:packaging", NS), "root packaging"),
    )
    expected_coordinates = (
        "io.oxidebatch.validation",
        "postgres-postgres-java-benchmark",
        "0.1.0",
        "pom",
    )
    if coordinates != expected_coordinates:
        raise ValidationError(f"unexpected Java reactor coordinates: {coordinates!r}")

    modules = [text(module, "module") for module in root.findall("m:modules/m:module", NS)]
    if modules != ["raw-jdbc"]:
        raise ValidationError(f"PR1 Java reactor must contain exactly raw-jdbc, got {modules!r}")

    release = text(root.find("m:properties/m:maven.compiler.release", NS), "maven.compiler.release")
    if release != "21":
        raise ValidationError(f"Java benchmark must compile for Java 21, got {release!r}")


def validate_raw(root: ET.Element) -> None:
    parent = root.find("m:parent", NS)
    if parent is None:
        raise ValidationError("raw-jdbc must inherit the reviewed local Java reactor parent")
    parent_coordinates = (
        text(parent.find("m:groupId", NS), "raw parent groupId"),
        text(parent.find("m:artifactId", NS), "raw parent artifactId"),
        text(parent.find("m:version", NS), "raw parent version"),
        text(parent.find("m:relativePath", NS), "raw parent relativePath"),
    )
    expected_parent = (
        "io.oxidebatch.validation",
        "postgres-postgres-java-benchmark",
        "0.1.0",
        "../pom.xml",
    )
    if parent_coordinates != expected_parent:
        raise ValidationError(f"unexpected raw-jdbc parent coordinates: {parent_coordinates!r}")

    artifact_id = text(root.find("m:artifactId", NS), "raw artifactId")
    packaging = text(root.find("m:packaging", NS), "raw packaging")
    if (artifact_id, packaging) != ("raw-jdbc", "jar"):
        raise ValidationError(f"unexpected raw-jdbc project identity: {(artifact_id, packaging)!r}")
    if root.find("m:dependencyManagement", NS) is not None:
        raise ValidationError("raw-jdbc dependencyManagement is forbidden in PR1")

    dependencies = []
    for dependency in root.findall("m:dependencies/m:dependency", NS):
        children = {child.tag.rsplit("}", 1)[-1] for child in dependency}
        if children != {"groupId", "artifactId", "version"}:
            raise ValidationError(f"raw-jdbc dependency contains unsupported controls: {sorted(children)!r}")
        dependencies.append(
            (
                text(dependency.find("m:groupId", NS), "dependency groupId"),
                text(dependency.find("m:artifactId", NS), "dependency artifactId"),
                text(dependency.find("m:version", NS), "dependency version"),
            )
        )

    expected = [("org.postgresql", "postgresql", "42.7.13")]
    if dependencies != expected:
        raise ValidationError(
            "raw-jdbc direct dependency surface must be exactly PostgreSQL JDBC 42.7.13; "
            f"got {dependencies!r}"
        )


def validate_source() -> None:
    source = (
        RAW_ROOT
        / "src/main/java/io/oxidebatch/workloads/postgres/benchmark/rawjdbc/RawJdbcMain.java"
    )
    if not source.is_file():
        raise ValidationError("missing raw-jdbc production source")
    content = source.read_text(encoding="utf-8")

    forbidden = {
        "OFFSET": "paging must use keyset pagination, never OFFSET",
        "executeBatch(": "primary writer must use one multi-row VALUES statement, not JDBC batching",
        "COPY ": "primary writer must not use PostgreSQL COPY",
        "org.springframework": "raw-jdbc source must not depend on Spring",
        "oxide_batch.": "raw-jdbc source must not access OxideBatch metadata",
        "System.exit(": "candidate failure controls must not self-terminate the process",
    }
    for needle, message in forbidden.items():
        if needle in content:
            raise ValidationError(message)

    required = {
        "LOCK TABLE app_source.source_customer IN SHARE MODE": "source stability lock",
        "source.setAutoCommit(false)": "autocommit=false cursor prerequisite",
        "destination.setAutoCommit(false)": "manual destination transaction boundary",
        "statement.setFetchSize(config.readBatchSize())": "bounded pgjdbc fetch",
        "WHERE customer_id > ?": "keyset/resume predicate",
        "benchmark_java.raw_checkpoint": "raw Java-owned checkpoint schema",
        'properties.setProperty("reWriteBatchedInserts", "false")': "rewrite shortcut disabled",
        "COLUMNS_PER_ROW = 7": "seven-column destination contract",
        "MAX_PARAMETERS_PER_STATEMENT = 2_000": "writer parameter parity bound",
        "ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW": "285-row bound derivation",
        "MAX_BOUND_PARAMETERS = ROWS_PER_STATEMENT * COLUMNS_PER_ROW": "1995-bind bound derivation",
    }
    for needle, label in required.items():
        if needle not in content:
            raise ValidationError(f"raw-jdbc source is missing required {label}")

    rows_per_statement = 2_000 // 7
    max_bound_parameters = rows_per_statement * 7
    statements_per_canonical_chunk = (1_000 + rows_per_statement - 1) // rows_per_statement
    if (rows_per_statement, max_bound_parameters, statements_per_canonical_chunk) != (285, 1_995, 4):
        raise ValidationError("internal writer parity arithmetic drifted unexpectedly")


def main() -> None:
    try:
        validate_manifest_inventory()
        validate_source_inventory()
        parent_path = JAVA_ROOT / "pom.xml"
        raw_path = RAW_ROOT / "pom.xml"
        parent = parse(parent_path)
        raw = parse(raw_path)
        validate_common(parent_path, parent)
        validate_common(raw_path, raw)
        validate_parent(parent)
        validate_raw(raw)
        validate_source()
    except ValidationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Java/Maven dependency and raw-JDBC parity boundary validation passed")


if __name__ == "__main__":
    main()
