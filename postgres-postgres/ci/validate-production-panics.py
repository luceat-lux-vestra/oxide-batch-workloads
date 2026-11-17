#!/usr/bin/env python3
"""Enforce the production unwrap/expect policy with Clippy's native test exceptions."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys
import tempfile


SUPPRESSION = re.compile(
    r"#\s*\[\s*(?:allow|expect)\s*\("
    r"(?:(?!\)\s*\]).)*"
    r"clippy::(?:unwrap_used|expect_used)"
    r"(?:(?!\)\s*\]).)*"
    r"\)\s*\]",
    re.DOTALL,
)

CLIPPY_CONFIG = """allow-expect-in-tests = true
allow-unwrap-in-tests = true
"""


def find_policy_suppressions(source_root: pathlib.Path) -> list[pathlib.Path]:
    offenders: list[pathlib.Path] = []
    if not source_root.is_dir():
        return offenders
    for path in sorted(source_root.rglob("*.rs")):
        text = path.read_text(encoding="utf-8")
        if SUPPRESSION.search(text):
            offenders.append(path)
    return offenders


def build_command(manifest_path: pathlib.Path, package: str) -> list[str]:
    return [
        "cargo",
        "clippy",
        "--locked",
        "--manifest-path",
        str(manifest_path),
        "--package",
        package,
        "--lib",
        "--bins",
        "--tests",
        "--no-deps",
        "--",
        "-D",
        "clippy::unwrap_used",
        "-D",
        "clippy::expect_used",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deny unwrap/expect in the guarded package while allowing Clippy's "
            "native test exceptions and respecting external macro lint scopes."
        )
    )
    parser.add_argument(
        "--manifest-path",
        type=pathlib.Path,
        required=True,
        help="Cargo.toml for the guarded package/workspace",
    )
    parser.add_argument(
        "--package",
        required=True,
        help="Cargo package whose src/ production policy is guarded",
    )
    args = parser.parse_args()

    manifest_path = args.manifest_path.resolve()
    if not manifest_path.is_file():
        parser.error(f"manifest does not exist: {manifest_path}")
    package_root = manifest_path.parent.resolve()

    suppressions = find_policy_suppressions(package_root / "src")
    if suppressions:
        print(
            "::error::source-level allow/expect for unwrap_used or expect_used "
            "would bypass the production panic policy",
            file=sys.stderr,
        )
        for path in suppressions:
            print(path.relative_to(package_root), file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="oxide-batch-clippy-conf-") as temp:
        conf_dir = pathlib.Path(temp)
        (conf_dir / "clippy.toml").write_text(CLIPPY_CONFIG, encoding="utf-8")

        env = os.environ.copy()
        env["CLIPPY_CONF_DIR"] = str(conf_dir)

        completed = subprocess.run(
            build_command(manifest_path, args.package),
            cwd=package_root,
            env=env,
            check=False,
        )
        return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
