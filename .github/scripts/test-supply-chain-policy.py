#!/usr/bin/env python3
"""Deterministic negative-policy proof for the canonical deny.toml (#32).

Builds two bounded, fully offline, self-contained fixture dependency graphs
in temporary directories -- never as permanent top-level Cargo projects in
this repository -- and proves the *actual* production `deny.toml` (not a
test-only rewritten copy) rejects each one via the real, installed
cargo-deny binary. Neither fixture depends on network access or a mutable
third-party repository: the disallowed dependency is a local `path` crate
(license fixture) or a local `file://` git repository this test creates and
commits itself (source fixture), so results are reproducible offline and
cannot later be invalidated by an upstream advisory being withdrawn or a
third-party repository changing.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DENY_TOML = REPO_ROOT / "deny.toml"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("cargo-deny"), "cargo-deny is not installed in this environment")
@unittest.skipUnless(shutil.which("cargo"), "cargo is not installed in this environment")
class DisallowedLicenseFixtureTests(unittest.TestCase):
    """Acceptance criterion 7.A: a deterministic license-policy violation."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

        dep_dir = self.root / "bad-license-dep"
        (dep_dir / "src").mkdir(parents=True)
        (dep_dir / "Cargo.toml").write_text(
            '[package]\nname = "bad-license-dep"\nversion = "0.1.0"\nedition = "2021"\nlicense = "GPL-3.0-only"\n',
            encoding="utf-8",
        )
        (dep_dir / "src" / "lib.rs").write_text("pub fn noop() {}\n", encoding="utf-8")

        self.main_dir = self.root / "main-crate"
        (self.main_dir / "src").mkdir(parents=True)
        (self.main_dir / "Cargo.toml").write_text(
            '[package]\nname = "main-crate"\nversion = "0.1.0"\nedition = "2021"\nlicense = "MIT"\n\n'
            '[dependencies]\nbad-license-dep = { path = "../bad-license-dep" }\n',
            encoding="utf-8",
        )
        (self.main_dir / "src" / "main.rs").write_text("fn main() { bad_license_dep::noop(); }\n", encoding="utf-8")

        lock = _run(["cargo", "generate-lockfile"], cwd=self.main_dir)
        self.assertEqual(lock.returncode, 0, lock.stderr)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_disallowed_license_is_rejected_by_the_real_canonical_policy(self) -> None:
        result = _run(
            [
                "cargo-deny",
                "--config",
                str(DENY_TOML),
                "--manifest-path",
                str(self.main_dir / "Cargo.toml"),
                "--locked",
                "--all-features",
                "check",
                "licenses",
            ],
            cwd=self.main_dir,
        )
        self.assertNotEqual(result.returncode, 0, "expected the real deny.toml to reject a GPL-3.0-only dependency")
        combined = result.stdout + result.stderr
        self.assertIn("GPL-3.0-only", combined)
        self.assertIn("bad-license-dep", combined)


@unittest.skipUnless(shutil.which("cargo-deny"), "cargo-deny is not installed in this environment")
@unittest.skipUnless(shutil.which("cargo"), "cargo is not installed in this environment")
@unittest.skipUnless(shutil.which("git"), "git is not installed in this environment")
class DisallowedGitSourceFixtureTests(unittest.TestCase):
    """Acceptance criterion 7.B: a deterministic source-policy violation.

    Uses a local `file://` git remote this test creates and commits itself,
    so the dependency resolves fully offline and is never at the mercy of a
    mutable third-party GitHub repository.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

        self.git_repo = self.root / "git-only-dep"
        (self.git_repo / "src").mkdir(parents=True)
        (self.git_repo / "Cargo.toml").write_text(
            '[package]\nname = "git-only-dep"\nversion = "0.1.0"\nedition = "2021"\nlicense = "MIT"\n',
            encoding="utf-8",
        )
        (self.git_repo / "src" / "lib.rs").write_text("pub fn noop() {}\n", encoding="utf-8")

        env_git = ["git", "-c", "user.email=supply-chain-test@example.com", "-c", "user.name=supply-chain-test"]
        self.assertEqual(_run(["git", "init", "-q", "-b", "main"], cwd=self.git_repo).returncode, 0)
        self.assertEqual(_run(env_git + ["add", "-A"], cwd=self.git_repo).returncode, 0)
        commit = _run(env_git + ["commit", "-q", "-m", "init"], cwd=self.git_repo)
        self.assertEqual(commit.returncode, 0, commit.stderr)

        self.consumer_dir = self.root / "consumer-crate"
        (self.consumer_dir / "src").mkdir(parents=True)
        (self.consumer_dir / "Cargo.toml").write_text(
            '[package]\nname = "consumer-crate"\nversion = "0.1.0"\nedition = "2021"\nlicense = "MIT"\n\n'
            f'[dependencies]\ngit-only-dep = {{ git = "file://{self.git_repo}" }}\n',
            encoding="utf-8",
        )
        (self.consumer_dir / "src" / "main.rs").write_text("fn main() { git_only_dep::noop(); }\n", encoding="utf-8")

        lock = _run(["cargo", "generate-lockfile"], cwd=self.consumer_dir)
        self.assertEqual(lock.returncode, 0, lock.stderr)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_disallowed_git_source_is_rejected_by_the_real_canonical_policy(self) -> None:
        result = _run(
            [
                "cargo-deny",
                "--config",
                str(DENY_TOML),
                "--manifest-path",
                str(self.consumer_dir / "Cargo.toml"),
                "--locked",
                "--all-features",
                "check",
                "sources",
            ],
            cwd=self.consumer_dir,
        )
        self.assertNotEqual(result.returncode, 0, "expected the real deny.toml to reject an unlisted git source")
        combined = result.stdout + result.stderr
        self.assertIn("git-only-dep", combined)
        self.assertIn("source-not-allowed", combined)


if __name__ == "__main__":
    unittest.main()
