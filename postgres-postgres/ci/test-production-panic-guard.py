#!/usr/bin/env python3
"""Behavioral regression tests for the production unwrap/expect guard."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap


PACKAGE = "panic-guard-fixture"
MACRO_PACKAGE = "panic-guard-proc-macro"


def write_base_package(root: pathlib.Path, lib_source: str, with_macro: bool = False) -> None:
    (root / "src").mkdir(parents=True)
    dependency_block = (
        f'\n[dependencies]\n{MACRO_PACKAGE} = {{ path = "../macro" }}\n'
        if with_macro
        else ""
    )
    (root / "Cargo.toml").write_text(
        textwrap.dedent(
            f"""\
            [package]
            name = "{PACKAGE}"
            version = "0.0.0"
            edition = "2021"

            [lib]
            path = "src/lib.rs"

            [[bin]]
            name = "{PACKAGE}"
            path = "src/main.rs"
            """
        ).rstrip()
        + dependency_block,
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(
        textwrap.dedent(lib_source),
        encoding="utf-8",
    )
    (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")


def write_proc_macro_package(root: pathlib.Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        textwrap.dedent(
            f"""\
            [package]
            name = "{MACRO_PACKAGE}"
            version = "0.0.0"
            edition = "2021"

            [lib]
            proc-macro = true
            """
        ),
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(
        textwrap.dedent(
            """
            use proc_macro::TokenStream;

            #[proc_macro_attribute]
            pub fn generated_expect(_attr: TokenStream, _item: TokenStream) -> TokenStream {
                let generated = r#"
                    #[allow(clippy::restriction)]
                    pub fn production_value() -> u8 {
                        Some(1_u8).expect("generated inside external proc macro")
                    }
                "#;
                generated.parse().unwrap_or_else(|_| TokenStream::new())
            }
            """
        ),
        encoding="utf-8",
    )


def generate_lockfile(manifest_path: pathlib.Path) -> None:
    completed = subprocess.run(
        [
            "cargo",
            "generate-lockfile",
            "--offline",
            "--manifest-path",
            str(manifest_path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        print(completed.stdout, file=sys.stderr)
        raise SystemExit(completed.returncode)


def run_validator(
    validator: pathlib.Path,
    manifest_path: pathlib.Path,
    should_pass: bool,
    name: str,
) -> None:
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(manifest_path.parent.parent / "target")
    completed = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--manifest-path",
            str(manifest_path),
            "--package",
            PACKAGE,
        ],
        cwd=manifest_path.parent,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    passed = completed.returncode == 0
    if passed != should_pass:
        expectation = "pass" if should_pass else "fail"
        print(
            f"::error::{name}: expected guard to {expectation}, "
            f"got exit {completed.returncode}",
            file=sys.stderr,
        )
        print(completed.stdout, file=sys.stderr)
        raise SystemExit(1)


def run_case(
    validator: pathlib.Path,
    name: str,
    lib_source: str,
    should_pass: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"panic-guard-{name}-") as temporary:
        root = pathlib.Path(temporary) / "app"
        write_base_package(root, lib_source)
        generate_lockfile(root / "Cargo.toml")
        run_validator(validator, root / "Cargo.toml", should_pass, name)


def run_proc_macro_case(
    validator: pathlib.Path,
    name: str,
    lib_source: str,
    should_pass: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"panic-guard-{name}-") as temporary:
        temp_root = pathlib.Path(temporary)
        macro_root = temp_root / "macro"
        app_root = temp_root / "app"
        write_proc_macro_package(macro_root)
        write_base_package(app_root, lib_source, with_macro=True)
        generate_lockfile(app_root / "Cargo.toml")
        run_validator(validator, app_root / "Cargo.toml", should_pass, name)


def main() -> int:
    validator = pathlib.Path(__file__).with_name("validate-production-panics.py")

    run_case(
        validator,
        "test-only-expect",
        """
        pub fn production_value() -> u8 {
            1
        }

        #[cfg(test)]
        mod tests {
            #[test]
            fn test_only_expect_is_allowed_by_clippy_config() {
                assert_eq!(Some(1_u8).expect("fixture value"), 1);
            }
        }
        """,
        should_pass=True,
    )

    run_proc_macro_case(
        validator,
        "external-proc-macro-generated-expect",
        f"""
        #[{MACRO_PACKAGE.replace("-", "_")}::generated_expect]
        pub fn replaced_by_macro() -> u8 {{
            1
        }}
        """,
        should_pass=True,
    )

    run_case(
        validator,
        "production-expect",
        """
        pub fn production_value() -> u8 {
            Some(1_u8).expect("production expect must be rejected")
        }
        """,
        should_pass=False,
    )

    run_case(
        validator,
        "production-unwrap",
        """
        pub fn production_value() -> u8 {
            Some(1_u8).unwrap()
        }
        """,
        should_pass=False,
    )

    run_case(
        validator,
        "source-suppression",
        """
        #[allow(clippy::expect_used)]
        pub fn production_value() -> u8 {
            Some(1_u8).expect("suppression must be rejected by policy guard")
        }
        """,
        should_pass=False,
    )

    print("production panic guard fixtures: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
