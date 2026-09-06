#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate-poms.py")
SPEC = importlib.util.spec_from_file_location("validate_poms", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def pom(xml_body: str) -> ET.Element:
    return ET.fromstring(
        '<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0">'
        + xml_body
        + "</project>"
    )


class ExactVersionTests(unittest.TestCase):
    def test_accepts_exact_release(self) -> None:
        validator.exact_version("42.7.13", "dependency")
        validator.exact_version("3.16.0", "plugin")

    def test_rejects_dynamic_and_snapshot_versions(self) -> None:
        for value in ("1.0-SNAPSHOT", "LATEST", "RELEASE", "[1.0,2.0)", "${pgjdbc.version}"):
            with self.subTest(value=value):
                with self.assertRaises(validator.ValidationError):
                    validator.exact_version(value, "dependency")


class ManifestBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_workload_root = validator.WORKLOAD_ROOT
        self.original_java_root = validator.JAVA_ROOT
        self.tempdir = tempfile.TemporaryDirectory()
        self.workload_root = Path(self.tempdir.name)
        self.java_root = self.workload_root / "benchmark" / "java"
        self.java_root.mkdir(parents=True)
        validator.WORKLOAD_ROOT = self.workload_root
        validator.JAVA_ROOT = self.java_root

    def tearDown(self) -> None:
        validator.WORKLOAD_ROOT = self.original_workload_root
        validator.JAVA_ROOT = self.original_java_root
        self.tempdir.cleanup()

    def write_expected_manifests(self) -> None:
        (self.java_root / "raw-jdbc").mkdir(parents=True)
        (self.java_root / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        (self.java_root / "raw-jdbc" / "pom.xml").write_text("<project/>\n", encoding="utf-8")

    def test_accepts_exact_manifest_inventory(self) -> None:
        self.write_expected_manifests()
        validator.validate_manifest_inventory()

    def test_rejects_unreviewed_extra_manifest_inside_java_root(self) -> None:
        self.write_expected_manifests()
        extra = self.java_root / "shadow"
        extra.mkdir()
        (extra / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        with self.assertRaisesRegex(validator.ValidationError, "manifest inventory"):
            validator.validate_manifest_inventory()

    def test_rejects_unreviewed_extra_manifest_elsewhere_in_workload(self) -> None:
        self.write_expected_manifests()
        extra = self.workload_root / "shadow"
        extra.mkdir()
        (extra / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        with self.assertRaisesRegex(validator.ValidationError, "manifest inventory"):
            validator.validate_manifest_inventory()

    def test_rejects_symlink_project_file(self) -> None:
        target = self.java_root / "real-pom.xml"
        target.write_text("<project/>\n", encoding="utf-8")
        linked = self.java_root / "pom.xml"
        linked.symlink_to(target)
        with self.assertRaisesRegex(validator.ValidationError, "must not be a symlink"):
            validator.parse(linked)


class EffectiveModelBoundaryTests(unittest.TestCase):
    def test_rejects_profile(self) -> None:
        root = pom("<profiles><profile><id>x</id></profile></profiles>")
        with self.assertRaisesRegex(validator.ValidationError, "profiles are forbidden"):
            validator.validate_common(validator.JAVA_ROOT / "pom.xml", root)

    def test_rejects_profile_repository(self) -> None:
        root = pom(
            "<profiles><profile><id>x</id><repositories><repository>"
            "<id>x</id><url>https://example.invalid</url>"
            "</repository></repositories></profile></profiles>"
        )
        with self.assertRaises(validator.ValidationError):
            validator.validate_common(validator.JAVA_ROOT / "pom.xml", root)

    def test_rejects_build_extension(self) -> None:
        root = pom(
            "<build><extensions><extension><groupId>x</groupId><artifactId>y</artifactId>"
            "<version>1.0.0</version></extension></extensions></build>"
        )
        with self.assertRaisesRegex(validator.ValidationError, "build extensions are forbidden"):
            validator.validate_common(validator.JAVA_ROOT / "pom.xml", root)

    def test_reactor_root_rejects_external_parent(self) -> None:
        root = pom("<parent><groupId>x</groupId><artifactId>y</artifactId><version>1.0.0</version></parent>")
        with self.assertRaisesRegex(validator.ValidationError, "must not inherit"):
            validator.validate_parent(root)

    def test_reactor_root_rejects_inherited_dependencies(self) -> None:
        root = pom("<dependencies><dependency><groupId>x</groupId><artifactId>y</artifactId><version>1.0.0</version></dependency></dependencies>")
        with self.assertRaisesRegex(validator.ValidationError, "must not contribute"):
            validator.validate_parent(root)

    def test_raw_rejects_wrong_parent_coordinates(self) -> None:
        root = pom(
            "<parent><groupId>x</groupId><artifactId>y</artifactId><version>1.0.0</version>"
            "<relativePath>../pom.xml</relativePath></parent><artifactId>raw-jdbc</artifactId><packaging>jar</packaging>"
        )
        with self.assertRaisesRegex(validator.ValidationError, "parent coordinates"):
            validator.validate_raw(root)

    def test_raw_rejects_dependency_management_override(self) -> None:
        root = pom(
            "<parent><groupId>io.oxidebatch.validation</groupId>"
            "<artifactId>postgres-postgres-java-benchmark</artifactId><version>0.1.0</version>"
            "<relativePath>../pom.xml</relativePath></parent>"
            "<artifactId>raw-jdbc</artifactId><packaging>jar</packaging>"
            "<dependencyManagement><dependencies><dependency><groupId>x</groupId>"
            "<artifactId>y</artifactId><version>1.0.0</version></dependency></dependencies></dependencyManagement>"
        )
        with self.assertRaisesRegex(validator.ValidationError, "dependencyManagement"):
            validator.validate_raw(root)

    def test_raw_rejects_dependency_shape_controls(self) -> None:
        root = pom(
            "<parent><groupId>io.oxidebatch.validation</groupId>"
            "<artifactId>postgres-postgres-java-benchmark</artifactId><version>0.1.0</version>"
            "<relativePath>../pom.xml</relativePath></parent>"
            "<artifactId>raw-jdbc</artifactId><packaging>jar</packaging>"
            "<dependencies><dependency><groupId>org.postgresql</groupId><artifactId>postgresql</artifactId>"
            "<version>42.7.13</version><scope>provided</scope></dependency></dependencies>"
        )
        with self.assertRaisesRegex(validator.ValidationError, "unsupported controls"):
            validator.validate_raw(root)


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_raw_root = validator.RAW_ROOT
        self.tempdir = tempfile.TemporaryDirectory()
        self.raw_root = Path(self.tempdir.name)
        source = (
            self.raw_root
            / "src/main/java/io/oxidebatch/workloads/postgres/benchmark/rawjdbc/RawJdbcMain.java"
        )
        source.parent.mkdir(parents=True)
        source.write_text(
            "\n".join(
                [
                    "LOCK TABLE app_source.source_customer IN SHARE MODE",
                    "source.setAutoCommit(false)",
                    "destination.setAutoCommit(false)",
                    "statement.setFetchSize(config.readBatchSize())",
                    "WHERE customer_id > ?",
                    "benchmark_java.raw_checkpoint",
                    'properties.setProperty(\"reWriteBatchedInserts\", \"false\")',
                    "COLUMNS_PER_ROW = 7",
                    "MAX_PARAMETERS_PER_STATEMENT = 2_000",
                    "ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW",
                    "MAX_BOUND_PARAMETERS = ROWS_PER_STATEMENT * COLUMNS_PER_ROW",
                ]
            ),
            encoding="utf-8",
        )
        validator.RAW_ROOT = self.raw_root

    def tearDown(self) -> None:
        validator.RAW_ROOT = self.original_raw_root
        self.tempdir.cleanup()

    def test_accepts_required_boundary_markers(self) -> None:
        validator.validate_source()

    def test_rejects_unreviewed_extra_java_source(self) -> None:
        extra = self.raw_root / "src/main/java/Shadow.java"
        extra.write_text("final class Shadow {}\n", encoding="utf-8")
        with self.assertRaisesRegex(validator.ValidationError, "source inventory"):
            validator.validate_source_inventory()

    def test_rejects_offset_paging(self) -> None:
        source = next(self.raw_root.rglob("RawJdbcMain.java"))
        source.write_text(source.read_text(encoding="utf-8") + "\nOFFSET\n", encoding="utf-8")
        with self.assertRaisesRegex(validator.ValidationError, "keyset"):
            validator.validate_source()

    def test_rejects_jdbc_batch_writer_shape(self) -> None:
        source = next(self.raw_root.rglob("RawJdbcMain.java"))
        source.write_text(
            source.read_text(encoding="utf-8") + "\nexecuteBatch(\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.ValidationError, "JDBC batching"):
            validator.validate_source()


if __name__ == "__main__":
    unittest.main()
