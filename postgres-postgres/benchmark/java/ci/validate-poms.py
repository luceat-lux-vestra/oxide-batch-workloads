#!/usr/bin/env python3
"""Fail-closed Maven inventory and JBeret PR1 boundary checks."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

JAVA_ROOT = Path(__file__).resolve().parents[1]
WORKLOAD_ROOT = JAVA_ROOT.parent.parent
RAW_ROOT = JAVA_ROOT / "raw-jdbc"
SPRING_ROOT = JAVA_ROOT / "spring-batch"
JBERET_ROOT = JAVA_ROOT / "jberet"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
JSL_NS = {"j": "https://jakarta.ee/xml/ns/jakartaee"}
DYNAMIC = re.compile(r"(?:SNAPSHOT|LATEST|RELEASE|[\[\]\(\),])", re.IGNORECASE)

EXPECTED_MANIFESTS = (
    "benchmark/java/jberet/pom.xml",
    "benchmark/java/pom.xml",
    "benchmark/java/raw-jdbc/pom.xml",
    "benchmark/java/spring-batch/pom.xml",
)
EXPECTED_JBERET_SOURCE = (
    "src/main/java/io/oxidebatch/workloads/postgres/benchmark/jberet/JBeretMain.java",
)
EXPECTED_JBERET_RESOURCES = (
    "src/main/resources/META-INF/batch-jobs/postgres-postgres.xml",
    "src/main/resources/META-INF/beans.xml",
    "src/main/resources/jberet.properties",
)
EXPECTED_LOCAL_PARENT = (
    "io.oxidebatch.validation",
    "postgres-postgres-java-benchmark",
    "0.1.0",
    "../pom.xml",
)
EXPECTED_JBERET_DEPENDENCIES = [
    ("org.jberet", "jberet-se", "3.2.0.Final"),
    ("jakarta.batch", "jakarta.batch-api", "2.1.1"),
    ("jakarta.inject", "jakarta.inject-api", "2.0.1.MR"),
    ("jakarta.enterprise", "jakarta.enterprise.cdi-api", "4.1.0"),
    ("jakarta.annotation", "jakarta.annotation-api", "3.0.0"),
    ("jakarta.transaction", "jakarta.transaction-api", "2.0.1"),
    ("org.jboss.logging", "jboss-logging", "3.6.3.Final"),
    ("org.jboss.marshalling", "jboss-marshalling", "2.3.0"),
    ("org.jboss.weld", "weld-core-impl", "5.1.7.Final"),
    ("org.jboss.weld.se", "weld-se-core", "5.1.7.Final"),
    ("org.wildfly.security", "wildfly-elytron-security-manager", "2.9.2.Final"),
    ("org.wildfly.security", "wildfly-elytron-security-manager-action", "2.9.2.Final"),
    ("org.postgresql", "postgresql", "42.7.13"),
]


class ValidationError(ValueError):
    pass


def text(node: ET.Element | None, field: str) -> str:
    if node is None or node.text is None or not node.text.strip():
        raise ValidationError(f"missing {field}")
    return node.text.strip()


def exact_version(value: str, field: str) -> None:
    if "${" in value or DYNAMIC.search(value):
        raise ValidationError(f"{field} must use an exact literal release version, got {value!r}")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z_-]+)+", value):
        raise ValidationError(f"{field} is not an accepted exact version literal: {value!r}")


def parse(path: Path) -> ET.Element:
    if path.is_symlink():
        raise ValidationError(f"project/resource file must not be a symlink: {path.relative_to(JAVA_ROOT)}")
    if not path.is_file():
        raise ValidationError(f"missing project/resource file: {path.relative_to(JAVA_ROOT)}")
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValidationError(f"invalid XML in {path.relative_to(JAVA_ROOT)}: {exc}") from exc


def validate_manifest_inventory() -> None:
    actual = tuple(sorted(str(path.relative_to(WORKLOAD_ROOT)) for path in WORKLOAD_ROOT.rglob("pom.xml")))
    if actual != EXPECTED_MANIFESTS:
        raise ValidationError(f"Maven manifest inventory must be exactly {EXPECTED_MANIFESTS!r}, got {actual!r}")


def validate_jberet_inventory() -> None:
    source_root = JBERET_ROOT / "src/main/java"
    sources = tuple(sorted(str(path.relative_to(JBERET_ROOT)) for path in source_root.rglob("*.java")))
    if sources != EXPECTED_JBERET_SOURCE:
        raise ValidationError(f"JBeret source inventory must be exactly {EXPECTED_JBERET_SOURCE!r}, got {sources!r}")
    resource_root = JBERET_ROOT / "src/main/resources"
    resources = tuple(
        sorted(str(path.relative_to(JBERET_ROOT)) for path in resource_root.rglob("*") if path.is_file())
    )
    if resources != EXPECTED_JBERET_RESOURCES:
        raise ValidationError(
            f"JBeret resource inventory must be exactly {EXPECTED_JBERET_RESOURCES!r}, got {resources!r}"
        )


def validate_common(path: Path, root: ET.Element) -> None:
    if root.find("m:profiles", NS) is not None:
        raise ValidationError(f"Maven profiles are forbidden: {path.relative_to(JAVA_ROOT)}")
    if root.find(".//m:repositories", NS) is not None:
        raise ValidationError(f"custom Maven repositories are forbidden: {path.relative_to(JAVA_ROOT)}")
    if root.find(".//m:pluginRepositories", NS) is not None:
        raise ValidationError(f"custom Maven pluginRepositories are forbidden: {path.relative_to(JAVA_ROOT)}")
    if root.find(".//m:build/m:extensions/m:extension", NS) is not None:
        raise ValidationError(f"Maven build extensions are forbidden: {path.relative_to(JAVA_ROOT)}")
    project_version = root.find("m:version", NS)
    if project_version is not None:
        exact_version(text(project_version, "project version"), f"{path.name} project version")
    parent = root.find("m:parent", NS)
    if parent is not None:
        exact_version(text(parent.find("m:version", NS), "parent version"), f"{path.name} parent version")
    for dependency in root.findall(".//m:dependencies/m:dependency", NS):
        group = text(dependency.find("m:groupId", NS), "dependency groupId")
        artifact = text(dependency.find("m:artifactId", NS), "dependency artifactId")
        version = text(dependency.find("m:version", NS), f"dependency version for {group}:{artifact}")
        exact_version(version, f"dependency {group}:{artifact}")
    for plugin in root.findall(".//m:build/m:plugins/m:plugin", NS):
        group_node = plugin.find("m:groupId", NS)
        group = text(group_node, "plugin groupId") if group_node is not None else "org.apache.maven.plugins"
        artifact = text(plugin.find("m:artifactId", NS), "plugin artifactId")
        version = text(plugin.find("m:version", NS), f"plugin version for {group}:{artifact}")
        exact_version(version, f"plugin {group}:{artifact}")


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
    if coordinates != ("io.oxidebatch.validation", "postgres-postgres-java-benchmark", "0.1.0", "pom"):
        raise ValidationError(f"unexpected Java reactor coordinates: {coordinates!r}")
    modules = [text(module, "module") for module in root.findall("m:modules/m:module", NS)]
    if modules != ["raw-jdbc", "spring-batch", "jberet"]:
        raise ValidationError(f"Java reactor module order must be raw-jdbc, spring-batch, jberet; got {modules!r}")
    if text(root.find("m:properties/m:maven.compiler.release", NS), "maven.compiler.release") != "25":
        raise ValidationError("Java benchmark must compile for Java 25")


def validate_local_module(root: ET.Element, label: str, artifact: str) -> None:
    parent = root.find("m:parent", NS)
    if parent is None:
        raise ValidationError(f"{label} must inherit the reviewed local Java reactor parent")
    actual_parent = (
        text(parent.find("m:groupId", NS), f"{label} parent groupId"),
        text(parent.find("m:artifactId", NS), f"{label} parent artifactId"),
        text(parent.find("m:version", NS), f"{label} parent version"),
        text(parent.find("m:relativePath", NS), f"{label} parent relativePath"),
    )
    if actual_parent != EXPECTED_LOCAL_PARENT:
        raise ValidationError(f"unexpected {label} parent coordinates: {actual_parent!r}")
    identity = (
        text(root.find("m:artifactId", NS), f"{label} artifactId"),
        text(root.find("m:packaging", NS), f"{label} packaging"),
    )
    if identity != (artifact, "jar"):
        raise ValidationError(f"unexpected {label} project identity: {identity!r}")
    if root.find("m:dependencyManagement", NS) is not None:
        raise ValidationError(f"{label} dependencyManagement is forbidden")


def direct_dependencies(root: ET.Element, label: str) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for dependency in root.findall("m:dependencies/m:dependency", NS):
        children = {child.tag.rsplit("}", 1)[-1] for child in dependency}
        if children != {"groupId", "artifactId", "version"}:
            raise ValidationError(f"{label} dependency contains unsupported controls: {sorted(children)!r}")
        result.append(
            (
                text(dependency.find("m:groupId", NS), "dependency groupId"),
                text(dependency.find("m:artifactId", NS), "dependency artifactId"),
                text(dependency.find("m:version", NS), "dependency version"),
            )
        )
    return result


def validate_raw(root: ET.Element) -> None:
    validate_local_module(root, "raw-jdbc", "raw-jdbc")
    expected = [("org.postgresql", "postgresql", "42.7.13")]
    actual = direct_dependencies(root, "raw-jdbc")
    if actual != expected:
        raise ValidationError(f"raw-jdbc direct dependency surface must be exactly {expected!r}, got {actual!r}")


def validate_spring(root: ET.Element) -> None:
    validate_local_module(root, "spring-batch", "spring-batch")
    expected = [
        ("org.springframework.batch", "spring-batch-core", "6.0.5"),
        ("org.postgresql", "postgresql", "42.7.13"),
    ]
    actual = direct_dependencies(root, "spring-batch")
    if actual != expected:
        raise ValidationError(f"spring-batch direct dependency surface must be exactly {expected!r}, got {actual!r}")

    plugins = {
        (
            text(plugin.find("m:groupId", NS), "plugin groupId")
            if plugin.find("m:groupId", NS) is not None
            else "org.apache.maven.plugins",
            text(plugin.find("m:artifactId", NS), "plugin artifactId"),
        ): plugin
        for plugin in root.findall("m:build/m:plugins/m:plugin", NS)
    }
    enforcer = plugins.get(("org.apache.maven.plugins", "maven-enforcer-plugin"))
    if enforcer is None:
        raise ValidationError("spring-batch must explicitly scope the reviewed convergence exception")
    executions = enforcer.findall("m:executions/m:execution", NS)
    if len(executions) != 1:
        raise ValidationError("spring-batch enforcer override must contain exactly one execution")
    execution = executions[0]
    if text(execution.find("m:id", NS), "enforcer execution id") != "enforce-java-build-contract":
        raise ValidationError("spring-batch enforcer override must target enforce-java-build-contract")
    excludes = [
        text(exclude, "dependency convergence exclude")
        for exclude in execution.findall(
            "m:configuration/m:rules/m:dependencyConvergence/m:excludes/m:exclude", NS
        )
    ]
    if excludes != ["org.jspecify:jspecify"]:
        raise ValidationError(
            "spring-batch dependencyConvergence exception must be exactly org.jspecify:jspecify; "
            f"got {excludes!r}"
        )
    upper_bound_includes = [
        text(include, "upper-bound dependency include")
        for include in execution.findall(
            "m:configuration/m:rules/m:requireUpperBoundDeps/m:includes/m:include", NS
        )
    ]
    if upper_bound_includes != ["org.jspecify:jspecify"]:
        raise ValidationError(
            "spring-batch requireUpperBoundDeps must check exactly org.jspecify:jspecify; "
            f"got {upper_bound_includes!r}"
        )


def validate_jberet(root: ET.Element) -> None:
    validate_local_module(root, "jberet", "jberet")
    actual = direct_dependencies(root, "jberet")
    if actual != EXPECTED_JBERET_DEPENDENCIES:
        raise ValidationError(
            f"JBeret direct dependency surface must be exactly {EXPECTED_JBERET_DEPENDENCIES!r}, got {actual!r}"
        )


def validate_jberet_source() -> None:
    content = (JBERET_ROOT / EXPECTED_JBERET_SOURCE[0]).read_text(encoding="utf-8")
    forbidden = {
        "OFFSET": "JBeret paging must use keyset pagination, never OFFSET",
        "executeBatch(": "JBeret primary writer must not use JDBC batching",
        "COPY ": "JBeret primary writer must not use PostgreSQL COPY",
        "org.springframework": "JBeret source must not depend on Spring",
        "oxide_batch.": "JBeret source must not access OxideBatch metadata",
        "System.exit(": "JBeret candidate must not self-terminate",
    }
    for needle, message in forbidden.items():
        if needle in content:
            raise ValidationError(message)
    normalized = content.upper()
    for verb in ("UPDATE", "INSERT INTO", "DELETE FROM"):
        if f"{verb} JBERET." in normalized:
            raise ValidationError("candidate must not directly mutate JBeret repository metadata")
    required = {
        'JBERET_VERSION = "3.2.0.Final"': "exact JBeret version assertion",
        'PROVISIONAL_WRITER_COMMIT_MODEL = "writer-local-commit"': "provisional commit classification",
        "LOCK TABLE app_source.source_customer IN SHARE MODE": "source stability lock",
        "BatchRuntime.getJobOperator()": "public JobOperator acquisition",
        "operator.start(JOB_XML_NAME, parameters)": "public job start path",
        "CREATE SCHEMA IF NOT EXISTS jberet": "repository schema isolation",
        "EXPECTED_REPOSITORY_TABLES": "repository inventory",
        "WHERE customer_id > ?": "checkpoint/keyset predicate",
        "ORDER BY customer_id LIMIT ?": "bounded keyset paging",
        "cursorStatement.setFetchSize(effectiveFetchSize)": "bounded cursor fetch",
        "page.setFetchSize(effectivePageSize)": "bounded paging fetch",
        "COLUMNS_PER_ROW = 7": "seven-column destination contract",
        "MAX_PARAMETERS_PER_STATEMENT = 2_000": "writer parameter bound",
        "ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW": "285-row derivation",
        "connection.commit()": "provisional writer-local commit",
        "connection.rollback()": "writer-local rollback",
        'properties.setProperty("reWriteBatchedInserts", "false")': "driver rewrite disablement",
    }
    for needle, meaning in required.items():
        if needle not in content:
            raise ValidationError(f"JBeret source is missing required {meaning}")


def parse_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValidationError(f"invalid JBeret property line: {raw!r}")
        key, value = line.split("=", 1)
        if key in values:
            raise ValidationError(f"duplicate JBeret property: {key}")
        values[key] = value
    return values


def validate_jberet_resources() -> None:
    expected = {
        "job-repository-type": "jdbc",
        "db-url": "${JBERET_DATABASE_URL:jdbc:postgresql://localhost:5434/postgres_postgres_workload}",
        "db-user": "${JBERET_DATABASE_USER:oxide_batch_workload}",
        "db-password": "${JBERET_DATABASE_PASSWORD:oxide_batch_workload}",
        "db-table-prefix": "jberet.",
        "thread-pool-type": "fixed",
        "thread-pool-core-size": "3",
    }
    actual = parse_properties(JBERET_ROOT / "src/main/resources/jberet.properties")
    if actual != expected:
        raise ValidationError(f"JBeret Java-SE repository configuration drifted: {actual!r}")

    beans = parse(JBERET_ROOT / "src/main/resources/META-INF/beans.xml")
    if beans.tag != "{https://jakarta.ee/xml/ns/jakartaee}beans":
        raise ValidationError("JBeret CDI descriptor must use the Jakarta EE namespace")
    expected_beans_attributes = {"version": "4.0", "bean-discovery-mode": "all"}
    if beans.attrib != expected_beans_attributes or list(beans):
        raise ValidationError(
            "JBeret CDI descriptor must be an empty CDI 4.0 full bean archive with bean-discovery-mode=all"
        )

    root = parse(JBERET_ROOT / "src/main/resources/META-INF/batch-jobs/postgres-postgres.xml")
    if root.tag != "{https://jakarta.ee/xml/ns/jakartaee}job":
        raise ValidationError("JBeret job XML must use Jakarta Batch namespace")
    if root.attrib.get("id") != "postgres-postgres" or root.attrib.get("version") != "2.0":
        raise ValidationError("JBeret job XML must use canonical id and JSL 2.0")
    chunk = root.find("j:step/j:chunk", JSL_NS)
    if chunk is None or chunk.attrib.get("item-count") != "#{jobParameters['chunkSize']}":
        raise ValidationError("JBeret chunk size must come from the recorded job parameter")
    expected_refs = {
        "reader": "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresReader",
        "processor": "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresProcessor",
        "writer": "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresWriter",
    }
    for element, expected_ref in expected_refs.items():
        node = chunk.find(f"j:{element}", JSL_NS)
        if node is None or node.attrib.get("ref") != expected_ref:
            raise ValidationError(f"JBeret job XML {element} ref drifted")


def main() -> None:
    try:
        validate_manifest_inventory()
        validate_jberet_inventory()
        paths = {
            "parent": JAVA_ROOT / "pom.xml",
            "raw": RAW_ROOT / "pom.xml",
            "spring": SPRING_ROOT / "pom.xml",
            "jberet": JBERET_ROOT / "pom.xml",
        }
        roots = {name: parse(path) for name, path in paths.items()}
        for name, path in paths.items():
            validate_common(path, roots[name])
        validate_parent(roots["parent"])
        validate_raw(roots["raw"])
        validate_spring(roots["spring"])
        validate_jberet(roots["jberet"])
        validate_jberet_source()
        validate_jberet_resources()
    except ValidationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Java/Maven inventory and JBeret PR1 boundary validation passed")


if __name__ == "__main__":
    main()
