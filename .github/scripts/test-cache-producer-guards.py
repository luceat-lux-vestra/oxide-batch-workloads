#!/usr/bin/env python3
"""Regression contract for dependency-scoped protected-main compiler cache seeds."""

from fnmatch import fnmatchcase
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
PRODUCERS = ("sccache-main-producer.yml", "sccache-final-main-producer.yml")
COMMON_EXPECTED_PATHS = {
    "csv-postgres/Cargo.toml",
    "csv-postgres/Cargo.lock",
    "csv-postgres/.cargo/**",
    "csv-postgres/rust-toolchain",
    "csv-postgres/rust-toolchain.toml",
    ".cargo/**",
    "rust-toolchain",
    "rust-toolchain.toml",
}
EXPECTED_PATHS = {
    "sccache-main-producer.yml": COMMON_EXPECTED_PATHS
    | {".github/workflows/sccache-main-producer.yml"},
    "sccache-final-main-producer.yml": COMMON_EXPECTED_PATHS
    | {
        ".github/workflows/sccache-final-main-producer.yml",
        "csv-postgres/ci/validate",
    },
}


def push_paths(workflow: str) -> set[str]:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    header = text.split("\njobs:", 1)[0]
    match = re.search(
        r"(?m)^  push:\n    branches: \[main\]\n(?:    #[^\n]*\n)*    paths:\n"
        r"((?:      - '[^']+'\n)+)",
        header,
    )
    assert match, f"{workflow}: trusted main push paths missing or widened"
    assert "  workflow_dispatch:" in header, f"{workflow}: missing manual recovery"
    paths = set(re.findall(r"(?m)^      - '([^']+)'$", match.group(1)))
    expected = EXPECTED_PATHS[workflow]
    assert paths == expected, f"{workflow}: unexpected seed inputs {paths ^ expected}"
    assert "  pull_request:" not in header
    return paths


def selected(path: str, patterns: set[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def test_producers() -> None:
    for filename in PRODUCERS:
        paths = push_paths(filename)
        for path in (
            "csv-postgres/Cargo.toml",
            "csv-postgres/Cargo.lock",
            ".cargo/config.toml",
            "csv-postgres/.cargo/config.toml",
            "rust-toolchain.toml",
        ):
            assert selected(path, paths), (filename, path)

        if filename == "sccache-main-producer.yml":
            assert selected(".github/workflows/sccache-main-producer.yml", paths), filename
            assert not selected(".github/workflows/sccache-final-main-producer.yml", paths), filename
            assert not selected("csv-postgres/ci/validate", paths), filename
        else:
            assert selected(".github/workflows/sccache-final-main-producer.yml", paths), filename
            assert not selected(".github/workflows/sccache-main-producer.yml", paths), filename
            assert selected("csv-postgres/ci/validate", paths), filename
        for path in (
            "csv-postgres/src/main.rs",
            "csv-postgres/tests/restart.rs",
            "postgres-postgres/Cargo.lock",
            "README.md",
            "docs/benchmark.md",
            ".github/workflows/hardening-drift-audit.yml",
            ".github/scripts/validate-workflow-security.py",
        ):
            assert not selected(path, paths), (filename, path)
        source = (WORKFLOWS / filename).read_text(encoding="utf-8")
        assert "Verify protected-main authority" in source
        assert "actions/cache/save@" in source
        assert "steps.restore-sccache.outputs.cache-hit != 'true'" in source


def test_consumers() -> None:
    for filename, prefix, step in (
        ("fast-branch.yml", "workloads-fast-sccache-v1-linux-arm64-", "restore-sccache"),
        ("ci.yml", "workloads-final-csv-postgres-sccache-v1-linux-arm64-", "restore-final-sccache"),
    ):
        source = (WORKFLOWS / filename).read_text(encoding="utf-8")
        assert "lock_prefix=" + prefix in source
        assert "restore-keys: |" in source
        assert "outputs.lock_prefix" in source
        assert f"steps.{step}.outputs.cache-matched-key != ''" in source
        assert f"steps.{step}.outputs.cache-matched-key == ''" in source
        assert "SCCACHE_LOCAL_RW_MODE=READ_ONLY" in source
        assert "RUSTC_WRAPPER=sccache" in source
        assert "cache-hit == 'true'" not in source
        assert "No compatible protected-main" in source


if __name__ == "__main__":
    test_producers()
    test_consumers()
    print("trusted-main dependency-scoped cache producer/consumer guards: ok")
