#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

BOUNDARY_PATH = Path(__file__).with_name("validate-established-boundaries.py")
BOUNDARY_SPEC = importlib.util.spec_from_file_location("validate_established_boundaries", BOUNDARY_PATH)
assert BOUNDARY_SPEC and BOUNDARY_SPEC.loader
boundary = importlib.util.module_from_spec(BOUNDARY_SPEC)
BOUNDARY_SPEC.loader.exec_module(boundary)

POM_PATH = Path(__file__).with_name("validate-poms.py")
POM_SPEC = importlib.util.spec_from_file_location("validate_poms_established", POM_PATH)
assert POM_SPEC and POM_SPEC.loader
poms = importlib.util.module_from_spec(POM_SPEC)
POM_SPEC.loader.exec_module(poms)


def pom(body: str) -> ET.Element:
    return ET.fromstring('<project xmlns="http://maven.apache.org/POM/4.0.0">' + body + '</project>')


def local_parent() -> str:
    return (
        '<parent><groupId>io.oxidebatch.validation</groupId>'
        '<artifactId>postgres-postgres-java-benchmark</artifactId>'
        '<version>0.1.0</version><relativePath>../pom.xml</relativePath></parent>'
    )


def dependency(group: str, artifact: str, version: str, extra: str = '') -> str:
    return (
        f'<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId>'
        f'<version>{version}</version>{extra}</dependency>'
    )


def reviewed_spring(dependencies: str | None = None) -> ET.Element:
    deps = dependencies or (
        dependency('org.springframework.batch', 'spring-batch-core', '6.0.5')
        + dependency('org.postgresql', 'postgresql', '42.7.13')
    )
    enforcer = (
        '<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>'
        '<artifactId>maven-enforcer-plugin</artifactId><version>3.6.3</version>'
        '<executions><execution><id>enforce-java-build-contract</id><configuration><rules>'
        '<dependencyConvergence><excludes><exclude>org.jspecify:jspecify</exclude></excludes></dependencyConvergence>'
        '<requireUpperBoundDeps><includes><include>org.jspecify:jspecify</include></includes></requireUpperBoundDeps>'
        '</rules></configuration></execution></executions></plugin></plugins></build>'
    )
    return pom(
        local_parent()
        + '<artifactId>spring-batch</artifactId><packaging>jar</packaging>'
        + f'<dependencies>{deps}</dependencies>{enforcer}'
    )


class EstablishedSourceBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = boundary.RAW_SOURCE.read_text(encoding='utf-8')
        cls.spring = boundary.SPRING_SOURCE.read_text(encoding='utf-8')

    def test_current_sources_preserve_established_contracts(self) -> None:
        boundary.validate_source_inventory()
        boundary.validate_raw_content(self.raw)
        boundary.validate_spring_content(self.spring)
        boundary.validate_arithmetic()

    def test_rejects_unreviewed_extra_raw_source(self) -> None:
        old_raw = boundary.RAW_ROOT
        old_source = boundary.RAW_SOURCE
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = root / boundary.EXPECTED_RAW_SOURCES[0]
            canonical.parent.mkdir(parents=True)
            canonical.write_text(self.raw, encoding='utf-8')
            (canonical.parent / 'Shadow.java').write_text('final class Shadow {}\n', encoding='utf-8')
            boundary.RAW_ROOT = root
            boundary.RAW_SOURCE = canonical
            try:
                with self.assertRaisesRegex(boundary.ValidationError, 'source inventory'):
                    boundary.validate_source_inventory()
            finally:
                boundary.RAW_ROOT = old_raw
                boundary.RAW_SOURCE = old_source

    def test_raw_rejects_offset(self) -> None:
        with self.assertRaisesRegex(boundary.ValidationError, 'keyset'):
            boundary.validate_raw_content(self.raw + '\nOFFSET 10\n')

    def test_raw_rejects_jdbc_batching(self) -> None:
        with self.assertRaisesRegex(boundary.ValidationError, 'JDBC batching'):
            boundary.validate_raw_content(self.raw + '\nexecuteBatch(\n')

    def test_raw_rejects_lost_fetch_bound(self) -> None:
        mutated = self.raw.replace('statement.setFetchSize(config.readBatchSize())', '// removed', 1)
        with self.assertRaisesRegex(boundary.ValidationError, 'bounded cursor fetch'):
            boundary.validate_raw_content(mutated)

    def test_spring_rejects_stock_jdbc_batch_writer(self) -> None:
        with self.assertRaisesRegex(boundary.ValidationError, 'JdbcBatchItemWriter'):
            boundary.validate_spring_content(self.spring + '\nJdbcBatchItemWriter\n')

    def test_spring_rejects_offset_paging(self) -> None:
        with self.assertRaisesRegex(boundary.ValidationError, 'never OFFSET'):
            boundary.validate_spring_content(self.spring + '\nOFFSET 10\n')

    def test_spring_rejects_private_commit(self) -> None:
        with self.assertRaisesRegex(boundary.ValidationError, 'private commit'):
            boundary.validate_spring_content(self.spring + '\nconnection.commit(\n')

    def test_spring_rejects_direct_metadata_dml(self) -> None:
        with self.assertRaisesRegex(boundary.ValidationError, 'metadata'):
            boundary.validate_spring_content(self.spring + '\nUPDATE spring_batch.BATCH_STEP_EXECUTION SET x=1\n')

    def test_spring_rejects_direct_metadata_dml_case_insensitively(self) -> None:
        with self.assertRaisesRegex(boundary.ValidationError, 'metadata'):
            boundary.validate_spring_content(
                self.spring + '\ninsert into SpRiNg_BaTcH.BATCH_JOB_EXECUTION values (...)\n'
            )

    def test_spring_rejects_lost_public_restart(self) -> None:
        mutated = self.spring.replace('operator.restart(last)', '// removed', 1)
        with self.assertRaisesRegex(boundary.ValidationError, 'public typed-failure restart'):
            boundary.validate_spring_content(mutated)

    def test_spring_rejects_one_reader_without_restart_state(self) -> None:
        mutated = self.spring.replace('.saveState(true)', '.saveState(false)', 1)
        with self.assertRaisesRegex(boundary.ValidationError, 'both Spring cursor and paging'):
            boundary.validate_spring_content(mutated)


class EstablishedPomBoundaryTests(unittest.TestCase):
    def test_rejects_symlink_project_file(self) -> None:
        old_java = poms.JAVA_ROOT
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'real-pom.xml'
            target.write_text('<project/>\n', encoding='utf-8')
            linked = root / 'pom.xml'
            linked.symlink_to(target)
            poms.JAVA_ROOT = root
            try:
                with self.assertRaisesRegex(poms.ValidationError, 'must not be a symlink'):
                    poms.parse(linked)
            finally:
                poms.JAVA_ROOT = old_java

    def test_rejects_profile_repository(self) -> None:
        root = pom(
            '<profiles><profile><id>x</id><repositories><repository>'
            '<id>x</id><url>https://example.invalid</url>'
            '</repository></repositories></profile></profiles>'
        )
        with self.assertRaises(poms.ValidationError):
            poms.validate_common(poms.JAVA_ROOT / 'pom.xml', root)

    def test_rejects_build_extension(self) -> None:
        root = pom(
            '<build><extensions><extension><groupId>x</groupId><artifactId>y</artifactId>'
            '<version>1.0.0</version></extension></extensions></build>'
        )
        with self.assertRaisesRegex(poms.ValidationError, 'build extensions are forbidden'):
            poms.validate_common(poms.JAVA_ROOT / 'pom.xml', root)

    def test_reactor_root_rejects_external_parent(self) -> None:
        with self.assertRaisesRegex(poms.ValidationError, 'must not inherit'):
            poms.validate_parent(pom('<parent><groupId>x</groupId><artifactId>y</artifactId><version>1.0.0</version></parent>'))

    def test_reactor_root_rejects_inherited_dependencies(self) -> None:
        root = pom('<dependencies>' + dependency('x', 'y', '1.0.0') + '</dependencies>')
        with self.assertRaisesRegex(poms.ValidationError, 'must not contribute'):
            poms.validate_parent(root)

    def test_raw_rejects_wrong_parent_coordinates(self) -> None:
        root = pom(
            '<parent><groupId>x</groupId><artifactId>y</artifactId><version>1.0.0</version>'
            '<relativePath>../pom.xml</relativePath></parent>'
            '<artifactId>raw-jdbc</artifactId><packaging>jar</packaging>'
        )
        with self.assertRaisesRegex(poms.ValidationError, 'parent coordinates'):
            poms.validate_raw(root)

    def test_raw_rejects_dependency_management_override(self) -> None:
        root = pom(
            local_parent()
            + '<artifactId>raw-jdbc</artifactId><packaging>jar</packaging>'
            + '<dependencyManagement><dependencies>' + dependency('x', 'y', '1.0.0')
            + '</dependencies></dependencyManagement>'
        )
        with self.assertRaisesRegex(poms.ValidationError, 'dependencyManagement'):
            poms.validate_raw(root)

    def test_raw_rejects_dependency_shape_controls(self) -> None:
        root = pom(
            local_parent()
            + '<artifactId>raw-jdbc</artifactId><packaging>jar</packaging><dependencies>'
            + dependency('org.postgresql', 'postgresql', '42.7.13', '<scope>provided</scope>')
            + '</dependencies>'
        )
        with self.assertRaisesRegex(poms.ValidationError, 'unsupported controls'):
            poms.validate_raw(root)

    def test_accepts_exact_spring_dependency_surface(self) -> None:
        poms.validate_spring(reviewed_spring())

    def test_rejects_wrong_spring_batch_version(self) -> None:
        deps = (
            dependency('org.springframework.batch', 'spring-batch-core', '6.0.4')
            + dependency('org.postgresql', 'postgresql', '42.7.13')
        )
        with self.assertRaisesRegex(poms.ValidationError, 'direct dependency surface'):
            poms.validate_spring(reviewed_spring(deps))

    def test_rejects_extra_spring_dependency(self) -> None:
        deps = (
            dependency('org.springframework.batch', 'spring-batch-core', '6.0.5')
            + dependency('org.postgresql', 'postgresql', '42.7.13')
            + dependency('example', 'shadow', '1.0.0')
        )
        with self.assertRaisesRegex(poms.ValidationError, 'direct dependency surface'):
            poms.validate_spring(reviewed_spring(deps))

    def test_rejects_spring_dependency_scope_override(self) -> None:
        deps = (
            dependency('org.springframework.batch', 'spring-batch-core', '6.0.5', '<scope>provided</scope>')
            + dependency('org.postgresql', 'postgresql', '42.7.13')
        )
        with self.assertRaisesRegex(poms.ValidationError, 'unsupported controls'):
            poms.validate_spring(reviewed_spring(deps))


if __name__ == '__main__':
    unittest.main()
