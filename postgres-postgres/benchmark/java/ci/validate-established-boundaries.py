#!/usr/bin/env python3
"""Preserve campaign #79 raw/Spring source proof obligations while extending the reactor."""

from __future__ import annotations

import sys
from pathlib import Path

JAVA_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = JAVA_ROOT / "raw-jdbc"
SPRING_ROOT = JAVA_ROOT / "spring-batch"
RAW_SOURCE = RAW_ROOT / "src/main/java/io/oxidebatch/workloads/postgres/benchmark/rawjdbc/RawJdbcMain.java"
SPRING_SOURCE = SPRING_ROOT / "src/main/java/io/oxidebatch/workloads/postgres/benchmark/springbatch/SpringBatchMain.java"
EXPECTED_RAW_SOURCES = (
    "src/main/java/io/oxidebatch/workloads/postgres/benchmark/rawjdbc/RawJdbcMain.java",
)
EXPECTED_SPRING_SOURCES = (
    "src/main/java/io/oxidebatch/workloads/postgres/benchmark/springbatch/SpringBatchMain.java",
)


class ValidationError(ValueError):
    pass


def require(content: str, markers: dict[str, str], label: str) -> None:
    for needle, meaning in markers.items():
        if needle not in content:
            raise ValidationError(f"{label} lost established {meaning}")


def forbid(content: str, markers: dict[str, str]) -> None:
    for needle, message in markers.items():
        if needle in content:
            raise ValidationError(message)


def validate_source_inventory() -> None:
    raw_sources = tuple(
        sorted(str(path.relative_to(RAW_ROOT)) for path in (RAW_ROOT / "src/main/java").rglob("*.java"))
    )
    if raw_sources != EXPECTED_RAW_SOURCES:
        raise ValidationError(
            f"raw-JDBC source inventory must be exactly {EXPECTED_RAW_SOURCES!r}, got {raw_sources!r}"
        )
    spring_sources = tuple(
        sorted(str(path.relative_to(SPRING_ROOT)) for path in (SPRING_ROOT / "src/main/java").rglob("*.java"))
    )
    if spring_sources != EXPECTED_SPRING_SOURCES:
        raise ValidationError(
            f"Spring Batch source inventory must be exactly {EXPECTED_SPRING_SOURCES!r}, got {spring_sources!r}"
        )


def validate_raw_content(content: str) -> None:
    forbid(content, {
        "OFFSET": "raw-JDBC paging must use keyset pagination, never OFFSET",
        "executeBatch(": "raw-JDBC primary writer must not use JDBC batching",
        "COPY ": "raw-JDBC primary writer must not use PostgreSQL COPY",
        "org.springframework": "raw-JDBC source must not depend on Spring",
        "oxide_batch.": "raw-JDBC source must not access OxideBatch metadata",
        "System.exit(": "raw-JDBC candidate must not self-terminate",
    })
    require(content, {
        "LOCK TABLE app_source.source_customer IN SHARE MODE": "source stability lock",
        "source.setAutoCommit(false)": "cursor autocommit=false prerequisite",
        "destination.setAutoCommit(false)": "destination transaction boundary",
        "statement.setFetchSize(config.readBatchSize())": "bounded cursor fetch",
        "WHERE customer_id > ?": "keyset/resume predicate",
        "benchmark_java.raw_checkpoint": "raw Java checkpoint ownership",
        'properties.setProperty("reWriteBatchedInserts", "false")': "driver rewrite disablement",
        "COLUMNS_PER_ROW = 7": "seven-column destination contract",
        "MAX_PARAMETERS_PER_STATEMENT = 2_000": "writer parameter bound",
        "ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW": "285-row bound derivation",
        "MAX_BOUND_PARAMETERS = ROWS_PER_STATEMENT * COLUMNS_PER_ROW": "1995-bind bound derivation",
    }, "raw-JDBC")


def validate_spring_content(content: str) -> None:
    forbid(content, {
        "JdbcBatchItemWriter": "primary Spring writer must not use JdbcBatchItemWriter",
        "executeBatch(": "primary Spring writer must not use JDBC batching",
        "COPY ": "primary Spring writer must not use PostgreSQL COPY",
        "OFFSET": "Spring paging must use the PostgreSQL paging provider, never OFFSET",
        "System.exit(": "Spring candidate must not self-terminate",
        ".commit(": "Spring writer/candidate must not own a private commit boundary",
        ".rollback(": "Spring writer/candidate must not own a private rollback boundary",
    })
    normalized = content.upper()
    for verb in ("UPDATE", "INSERT INTO", "DELETE FROM"):
        if f"{verb} SPRING_BATCH." in normalized:
            raise ValidationError("Spring candidate must not directly mutate Spring Batch metadata")
    require(content, {
        'SPRING_BATCH_VERSION = "6.0.5"': "exact Spring Batch runtime assertion",
        "LOCK TABLE app_source.source_customer IN SHARE MODE": "source stability lock",
        "new JdbcJobRepositoryFactoryBean()": "real JDBC JobRepository",
        'factory.setTablePrefix("spring_batch.BATCH_")': "Spring metadata ownership",
        "new TaskExecutorJobOperator()": "public JobOperator implementation",
        "operator.restart(last)": "public typed-failure restart path",
        "new JdbcCursorItemReaderBuilder<SourceRow>()": "Spring cursor reader",
        ".connectionAutoCommit(false)": "pgjdbc cursor autocommit prerequisite",
        ".fetchSize(fetchSize)": "bounded cursor fetch",
        "new JdbcPagingItemReaderBuilder<SourceRow>()": "Spring paging reader",
        ".pageSize(pageSize)": "bounded paging page size",
        ".fetchSize(pageSize)": "bounded paging fetch size",
        "new PostgresPagingQueryProvider()": "PostgreSQL paging provider",
        'Map.of("customer_id", Order.ASCENDING)': "unique paging sort key",
        ".saveState(true)": "restart state persistence",
        "implements ItemWriter<ProjectedRow>": "custom Spring parity writer",
        "new JdbcTemplate(dataSource)": "transaction-aware Spring JDBC writer",
        "COLUMNS_PER_ROW = 7": "seven-column destination contract",
        "MAX_PARAMETERS_PER_STATEMENT = 2_000": "writer parameter bound",
        "ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW": "285-row bound derivation",
        "MAX_BOUND_PARAMETERS = ROWS_PER_STATEMENT * COLUMNS_PER_ROW": "1995-bind bound derivation",
        'addString("source_digest", sourceDigest)': "source digest identifying parameter",
        'addString("reader_mode", config.readerMode().value)': "reader mode identifying parameter",
        'addString("definition_revision", config.definitionRevision())': "definition identifying parameter",
        "ResourceDatabasePopulator": "official Spring schema initializer",
        "schema-postgresql.sql": "official PostgreSQL metadata schema",
        "EXPECTED_BATCH_TABLES = 6": "complete metadata table inventory",
        "EXPECTED_BATCH_SEQUENCES = 3": "complete metadata sequence inventory",
        "partial Spring Batch metadata schema detected": "partial metadata fail-closed check",
        "assertBatchSchemaComplete(jdbc)": "post-initialization schema verification",
    }, "Spring Batch")
    if content.count(".saveState(true)") != 2:
        raise ValidationError("both Spring cursor and paging readers must persist restart state")


def validate_arithmetic() -> None:
    rows_per_statement = 2_000 // 7
    max_bound_parameters = rows_per_statement * 7
    statements_per_canonical_chunk = (1_000 + rows_per_statement - 1) // rows_per_statement
    if (rows_per_statement, max_bound_parameters, statements_per_canonical_chunk) != (285, 1_995, 4):
        raise ValidationError("established writer parity arithmetic drifted unexpectedly")


def main() -> None:
    try:
        validate_source_inventory()
        validate_raw_content(RAW_SOURCE.read_text(encoding="utf-8"))
        validate_spring_content(SPRING_SOURCE.read_text(encoding="utf-8"))
        validate_arithmetic()
    except ValidationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Established raw-JDBC/Spring Batch proof obligations preserved")


if __name__ == "__main__":
    main()
