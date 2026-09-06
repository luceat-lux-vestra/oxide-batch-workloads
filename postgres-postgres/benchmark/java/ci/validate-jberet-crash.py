#!/usr/bin/env python3
"""Fail-closed PR2 boundary checks for the JBeret external crash qualification."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

JAVA_ROOT = Path(__file__).resolve().parents[1]
JBERET_ROOT = JAVA_ROOT / "jberet"
MAIN_SOURCE = JBERET_ROOT / "src/main/java/io/oxidebatch/workloads/postgres/benchmark/jberet/JBeretMain.java"
MAIN_JSL = JBERET_ROOT / "src/main/resources/META-INF/batch-jobs/postgres-postgres.xml"
CRASH_SOURCE = JBERET_ROOT / "src/test/java/io/oxidebatch/workloads/postgres/benchmark/jberet/JBeretCrashMain.java"
CRASH_JSL = JBERET_ROOT / "src/test/resources/META-INF/batch-jobs/postgres-postgres-crash.xml"
CRASH_BEANS = JBERET_ROOT / "src/test/resources/META-INF/beans.xml"
NS = {"j": "https://jakarta.ee/xml/ns/jakartaee"}


class ValidationError(ValueError):
    pass


def validate_production_boundary(main_source: str, main_jsl: str) -> None:
    forbidden = ("CrashBoundaryListener", "pauseAtChunk", "pausePhase", "pauseMarker", "postgres-postgres-crash")
    for needle in forbidden:
        if needle in main_source or needle in main_jsl:
            raise ValidationError(f"PR2 instrumentation leaked into production candidate surface: {needle}")
    if "connection.commit();" not in main_source:
        raise ValidationError("PR1 writer-local commit model disappeared")
    if "INSERT INTO app_business.customer_projection" not in main_source:
        raise ValidationError("PR1 plain INSERT writer disappeared")


def validate_crash_source(content: str) -> None:
    required = {
        "implements ItemWriteListener": "test-only ItemWriteListener instrumentation",
        'pause("before-write")': "pre-write crash boundary",
        'pause("after-write")': "post-writer-return crash boundary",
        "new CountDownLatch(1).await()": "external-kill pause",
        "ProcessHandle.current().pid()": "marker PID evidence",
        "operator.start(JOB_XML_NAME, parameters)": "public start path",
        "operator.restart(executionId, restartParameters)": "public restart path",
        "operator.getParameters(executionId)": "persisted parameter lookup",
        "step.getMetrics()": "public durable metric lookup",
        "LOCK TABLE app_source.source_customer IN SHARE MODE": "source stability lock",
        "source digest changed for existing JBeret execution": "source-mutation fail closed",
        'restartParameters.setProperty("pauseAtChunk", "0")': "restart instrumentation disablement",
        'restartParameters.setProperty("pausePhase", "none")': "restart phase disablement",
    }
    for needle, meaning in required.items():
        if needle not in content:
            raise ValidationError(f"crash driver is missing required {meaning}")
    forbidden = {
        "System.exit(": "candidate or harness code must not self-terminate",
        "executeBatch(": "crash driver must not introduce JDBC batching",
        "ON CONFLICT": "crash qualification must not add UPSERT repair",
        "DO NOTHING": "crash qualification must not hide replay conflicts",
    }
    for needle, meaning in forbidden.items():
        if needle in content:
            raise ValidationError(meaning)
    normalized = content.upper()
    for verb in ("UPDATE", "INSERT INTO", "DELETE FROM", "TRUNCATE", "ALTER TABLE", "DROP TABLE"):
        if f"{verb} JBERET." in normalized:
            raise ValidationError("crash driver must not mutate JBeret repository metadata")


def validate_crash_jsl(content: str) -> None:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValidationError(f"invalid crash JSL: {exc}") from exc
    if root.tag != "{https://jakarta.ee/xml/ns/jakartaee}job" or root.attrib.get("id") != "postgres-postgres-crash":
        raise ValidationError("crash JSL must use the isolated postgres-postgres-crash job id")
    step = root.find("j:step", NS)
    if step is None:
        raise ValidationError("crash JSL must contain one step")
    listeners = step.findall("j:listeners/j:listener", NS)
    expected_listener = "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretCrashMain$CrashBoundaryListener"
    if len(listeners) != 1 or listeners[0].attrib.get("ref") != expected_listener:
        raise ValidationError("crash JSL must contain exactly the test-only boundary listener")
    chunk = step.find("j:chunk", NS)
    if chunk is None or chunk.attrib.get("item-count") != "#{jobParameters['chunkSize']}":
        raise ValidationError("crash JSL must preserve the recorded chunk size")
    expected_refs = {
        "reader": "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresReader",
        "processor": "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresProcessor",
        "writer": "io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresWriter",
    }
    for element, expected in expected_refs.items():
        node = chunk.find(f"j:{element}", NS)
        if node is None or node.attrib.get("ref") != expected:
            raise ValidationError(f"crash JSL {element} must reuse the PR1 production component exactly")


def validate_inventory() -> None:
    expected_sources = {CRASH_SOURCE}
    actual_sources = set((JBERET_ROOT / "src/test/java").rglob("*.java"))
    if actual_sources != expected_sources:
        raise ValidationError(f"unexpected JBeret test Java inventory: {sorted(map(str, actual_sources))}")
    expected_resources = {CRASH_JSL, CRASH_BEANS}
    actual_resources = {path for path in (JBERET_ROOT / "src/test/resources").rglob("*") if path.is_file()}
    if actual_resources != expected_resources:
        raise ValidationError(f"unexpected JBeret test resource inventory: {sorted(map(str, actual_resources))}")


def main() -> None:
    try:
        validate_inventory()
        validate_production_boundary(MAIN_SOURCE.read_text(encoding="utf-8"), MAIN_JSL.read_text(encoding="utf-8"))
        validate_crash_source(CRASH_SOURCE.read_text(encoding="utf-8"))
        validate_crash_jsl(CRASH_JSL.read_text(encoding="utf-8"))
        beans = CRASH_BEANS.read_text(encoding="utf-8")
        if 'bean-discovery-mode="all"' not in beans or 'version="4.0"' not in beans:
            raise ValidationError("test bean archive must be CDI 4.0 discovery-mode all")
    except (OSError, ValidationError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("JBeret PR2 test-only crash boundary validation passed")


if __name__ == "__main__":
    main()
