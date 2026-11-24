#!/usr/bin/env python3

import importlib.util
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parent / "validate-workflow-security.py"
SPEC = importlib.util.spec_from_file_location("validate_workflow_security", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ActionPinValidationTests(unittest.TestCase):
    def write_workflow(self, root: pathlib.Path, uses_values: list[str]) -> None:
        workflows = root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        lines = [
            "name: fixture",
            "on: workflow_dispatch",
            "jobs:",
            "  fixture:",
            "    runs-on: ubuntu-latest",
            "    steps:",
        ]
        for value in uses_values:
            lines.append(f"      - uses: {value}")
        (workflows / "fixture.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_same_repository_references_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.write_workflow(
                root,
                [
                    "./.github/actions/local-action",
                    "$/.github/workflows/local-workflow.yml",
                ],
            )
            MODULE.validate_action_pins(root)

    def test_unpinned_external_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.write_workflow(root, ["actions/checkout"])
            with self.assertRaisesRegex(
                MODULE.WorkflowSecurityError,
                "external action has no immutable ref",
            ):
                MODULE.validate_action_pins(root)

    def test_external_tag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.write_workflow(root, ["actions/checkout@v4"])
            with self.assertRaisesRegex(
                MODULE.WorkflowSecurityError,
                "external action ref is not a full SHA",
            ):
                MODULE.validate_action_pins(root)

    def test_external_full_sha_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.write_workflow(root, ["actions/checkout@" + "a" * 40])
            MODULE.validate_action_pins(root)


class CargoUpdaterOwnershipTests(unittest.TestCase):
    def test_actual_registered_roots(self):
        MODULE.validate_registered_cargo_updaters()

    def test_missing_duplicate_disabled_and_unfrozen_subject_rejected(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".github").mkdir()
            (root / "workloads.json").write_text(json.dumps({
                "workloads": [{"path": "work"}],
                "fixtures": [{"path": "fixture"}],
            }), encoding="utf-8")
            work = (
                "  - package-ecosystem: cargo\n"
                "    directory: /work\n"
                "    schedule:\n"
                "      interval: weekly\n"
                "    ignore:\n"
                '      - dependency-name: "oxide-batch*"\n'
            )
            fixture = work.replace("/work", "/fixture").split("    ignore:")[0]
            config = root / ".github" / "dependabot.yml"
            for invalid, expected in (
                (work, "missing native Cargo updater"),
                (work + work + fixture, "duplicate Cargo updater"),
                (work + fixture.replace("      interval: weekly", "      interval: weekly\n    open-pull-requests-limit: 0"), "disabled"),
                (work.replace("oxide-batch*", "other") + fixture, "subject must remain campaign-owned"),
                (work.replace("oxide-batch*", "*") + fixture, "blanket Cargo ignore"),
            ):
                config.write_text("version: 2\nupdates:\n" + invalid, encoding="utf-8")
                with self.assertRaisesRegex(MODULE.WorkflowSecurityError, expected):
                    MODULE.validate_registered_cargo_updaters(root)
            config.write_text("version: 2\nupdates:\n" + work + fixture, encoding="utf-8")
            MODULE.validate_registered_cargo_updaters(root)


if __name__ == "__main__":
    unittest.main()
