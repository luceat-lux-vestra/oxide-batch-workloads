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


def pom(body: str) -> ET.Element:
    return ET.fromstring(
        '<project xmlns="http://maven.apache.org/POM/4.0.0">' + body + '</project>'
    )


def local_parent() -> str:
    return (
        '<parent><groupId>io.oxidebatch.validation</groupId>'
        '<artifactId>postgres-postgres-java-benchmark</artifactId>'
        '<version>0.1.0</version><relativePath>../pom.xml</relativePath></parent>'
    )


def dependencies(values: list[tuple[str, str, str]]) -> str:
    return ''.join(
        f'<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId>'
        f'<version>{version}</version></dependency>'
        for group, artifact, version in values
    )


class ExactVersionTests(unittest.TestCase):
    def test_accepts_release_literals(self) -> None:
        for value in ('42.7.13', '3.2.0.Final', '2.0.1.MR', '5.1.7.Final', '2.9.2.Final'):
            with self.subTest(value=value):
                validator.exact_version(value, 'version')

    def test_rejects_dynamic_or_indirected_versions(self) -> None:
        for value in ('1.0-SNAPSHOT', 'LATEST', 'RELEASE', '[1.0,2.0)', '${version}'):
            with self.subTest(value=value), self.assertRaises(validator.ValidationError):
                validator.exact_version(value, 'version')


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_workload, self.old_java = validator.WORKLOAD_ROOT, validator.JAVA_ROOT
        self.temp = tempfile.TemporaryDirectory()
        self.workload = Path(self.temp.name)
        self.java = self.workload / 'benchmark/java'
        self.java.mkdir(parents=True)
        validator.WORKLOAD_ROOT, validator.JAVA_ROOT = self.workload, self.java

    def tearDown(self) -> None:
        validator.WORKLOAD_ROOT, validator.JAVA_ROOT = self.old_workload, self.old_java
        self.temp.cleanup()

    def write_expected(self) -> None:
        (self.java / 'pom.xml').write_text('<project/>\n', encoding='utf-8')
        for module in ('raw-jdbc', 'spring-batch', 'jberet'):
            path = self.java / module
            path.mkdir(parents=True)
            (path / 'pom.xml').write_text('<project/>\n', encoding='utf-8')

    def test_accepts_exact_manifest_inventory(self) -> None:
        self.write_expected()
        validator.validate_manifest_inventory()

    def test_rejects_extra_manifest(self) -> None:
        self.write_expected()
        extra = self.workload / 'shadow'
        extra.mkdir()
        (extra / 'pom.xml').write_text('<project/>\n', encoding='utf-8')
        with self.assertRaisesRegex(validator.ValidationError, 'manifest inventory'):
            validator.validate_manifest_inventory()


class CommonPomTests(unittest.TestCase):
    def test_rejects_profile(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, 'profiles are forbidden'):
            validator.validate_common(
                validator.JAVA_ROOT / 'pom.xml',
                pom('<profiles><profile><id>x</id></profile></profiles>'),
            )

    def test_rejects_custom_repository(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, 'custom Maven repositories'):
            validator.validate_common(
                validator.JAVA_ROOT / 'pom.xml',
                pom('<repositories><repository><id>x</id><url>https://example.invalid</url></repository></repositories>'),
            )

    def test_parent_requires_exact_module_order(self) -> None:
        prefix = (
            '<groupId>io.oxidebatch.validation</groupId>'
            '<artifactId>postgres-postgres-java-benchmark</artifactId>'
            '<version>0.1.0</version><packaging>pom</packaging>'
        )
        suffix = '<properties><maven.compiler.release>25</maven.compiler.release></properties>'
        validator.validate_parent(
            pom(prefix + '<modules><module>raw-jdbc</module><module>spring-batch</module><module>jberet</module></modules>' + suffix)
        )
        with self.assertRaisesRegex(validator.ValidationError, 'module order'):
            validator.validate_parent(
                pom(prefix + '<modules><module>raw-jdbc</module><module>jberet</module><module>spring-batch</module></modules>' + suffix)
            )


class JBeretDependencyTests(unittest.TestCase):
    def reviewed(self, values: list[tuple[str, str, str]] | None = None) -> ET.Element:
        values = validator.EXPECTED_JBERET_DEPENDENCIES if values is None else values
        return pom(
            local_parent()
            + '<artifactId>jberet</artifactId><packaging>jar</packaging><dependencies>'
            + dependencies(values)
            + '</dependencies>'
        )

    def assert_surface_rejected(self, values: list[tuple[str, str, str]]) -> None:
        with self.assertRaisesRegex(validator.ValidationError, 'direct dependency surface'):
            validator.validate_jberet(self.reviewed(values))

    def test_accepts_exact_java_se_runtime_surface(self) -> None:
        validator.validate_jberet(self.reviewed())

    def test_rejects_missing_wildfly_security_manager(self) -> None:
        self.assert_surface_rejected([
            dep for dep in validator.EXPECTED_JBERET_DEPENDENCIES
            if dep[1] != 'wildfly-elytron-security-manager'
        ])

    def test_rejects_missing_weld_se(self) -> None:
        self.assert_surface_rejected([
            dep for dep in validator.EXPECTED_JBERET_DEPENDENCIES if dep[1] != 'weld-se-core'
        ])

    def test_rejects_wrong_runtime_version(self) -> None:
        self.assert_surface_rejected([
            (group, artifact, '5.1.6.Final' if artifact == 'weld-se-core' else version)
            for group, artifact, version in validator.EXPECTED_JBERET_DEPENDENCIES
        ])

    def test_rejects_dependency_scope_override(self) -> None:
        root = pom(
            local_parent()
            + '<artifactId>jberet</artifactId><packaging>jar</packaging><dependencies>'
            + '<dependency><groupId>org.jberet</groupId><artifactId>jberet-se</artifactId>'
            + '<version>3.2.0.Final</version><scope>provided</scope></dependency></dependencies>'
        )
        with self.assertRaisesRegex(validator.ValidationError, 'unsupported controls'):
            validator.validate_jberet(root)


class JBeretSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (validator.JBERET_ROOT / validator.EXPECTED_JBERET_SOURCE[0]).read_text(encoding='utf-8')

    def with_mutated_source(self, addition: str, pattern: str) -> None:
        old_root = validator.JBERET_ROOT
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / validator.EXPECTED_JBERET_SOURCE[0]
            path.parent.mkdir(parents=True)
            path.write_text(self.source + addition, encoding='utf-8')
            validator.JBERET_ROOT = root
            try:
                with self.assertRaisesRegex(validator.ValidationError, pattern):
                    validator.validate_jberet_source()
            finally:
                validator.JBERET_ROOT = old_root

    def test_current_source_satisfies_boundary(self) -> None:
        validator.validate_jberet_source()

    def test_rejects_offset(self) -> None:
        self.with_mutated_source('\nOFFSET 1\n', 'keyset')

    def test_rejects_direct_metadata_dml(self) -> None:
        self.with_mutated_source("\nUPDATE JbErEt.job_execution SET batchstatus='X'\n", 'metadata')

    def test_rejects_jdbc_batching(self) -> None:
        self.with_mutated_source('\nexecuteBatch(\n', 'JDBC batching')


class JBeretResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_root = validator.JBERET_ROOT
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        validator.JBERET_ROOT = self.root
        resources = self.root / 'src/main/resources'
        resources.mkdir(parents=True)
        (resources / 'jberet.properties').write_text(
            'job-repository-type=jdbc\n'
            'db-url=${JBERET_DATABASE_URL:jdbc:postgresql://localhost:5434/postgres_postgres_workload}\n'
            'db-user=${JBERET_DATABASE_USER:oxide_batch_workload}\n'
            'db-password=${JBERET_DATABASE_PASSWORD:oxide_batch_workload}\n'
            'db-table-prefix=jberet.\n'
            'thread-pool-type=fixed\n'
            'thread-pool-core-size=3\n',
            encoding='utf-8',
        )
        meta = resources / 'META-INF'
        meta.mkdir(parents=True)
        (meta / 'beans.xml').write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<beans xmlns="https://jakarta.ee/xml/ns/jakartaee" version="4.0" bean-discovery-mode="all"/>\n',
            encoding='utf-8',
        )
        job = meta / 'batch-jobs/postgres-postgres.xml'
        job.parent.mkdir(parents=True)
        job.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<job id="postgres-postgres" xmlns="https://jakarta.ee/xml/ns/jakartaee" version="2.0" restartable="true">\n'
            '  <step id="postgres-postgres-step"><chunk item-count="#{jobParameters[\'chunkSize\']}">\n'
            '    <reader ref="io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresReader"/>\n'
            '    <processor ref="io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresProcessor"/>\n'
            '    <writer ref="io.oxidebatch.workloads.postgres.benchmark.jberet.JBeretMain$PostgresWriter"/>\n'
            '  </chunk></step>\n</job>\n',
            encoding='utf-8',
        )

    def tearDown(self) -> None:
        validator.JBERET_ROOT = self.old_root
        self.temp.cleanup()

    def test_accepts_reviewed_resources(self) -> None:
        validator.validate_jberet_resources()

    def test_rejects_in_memory_repository(self) -> None:
        path = self.root / 'src/main/resources/jberet.properties'
        path.write_text(
            path.read_text(encoding='utf-8').replace('job-repository-type=jdbc', 'job-repository-type=in-memory'),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(validator.ValidationError, 'configuration drifted'):
            validator.validate_jberet_resources()

    def test_rejects_annotated_only_cdi_archive(self) -> None:
        path = self.root / 'src/main/resources/META-INF/beans.xml'
        path.write_text(
            path.read_text(encoding='utf-8').replace('bean-discovery-mode="all"', 'bean-discovery-mode="annotated"'),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(validator.ValidationError, 'full bean archive'):
            validator.validate_jberet_resources()


if __name__ == '__main__':
    unittest.main()
