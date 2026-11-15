#!/usr/bin/env python3
import copy
import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("allocator_registry.py")
SPEC = importlib.util.spec_from_file_location("allocator_registry", MODULE_PATH)
assert SPEC and SPEC.loader
allocator_registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(allocator_registry)


class AllocatorRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = allocator_registry.load_registry()

    def test_current_active_contract_is_registry_owned(self) -> None:
        self.assertEqual(
            [candidate["identity"] for candidate in allocator_registry.active_candidates(self.registry)],
            ["system", "jemalloc", "mimalloc", "snmalloc"],
        )
        self.assertEqual(
            allocator_registry.replacement_features(self.registry),
            ("allocator-jemalloc", "allocator-mimalloc", "allocator-snmalloc"),
        )

    def test_remaining_candidates_do_not_activate_before_qualification(self) -> None:
        self.assertEqual(
            [candidate["identity"] for candidate in self.registry["candidates"][4:]],
            ["tcmalloc", "rpmalloc"],
        )
        self.assertEqual(
            [candidate["identity"] for candidate in allocator_registry.active_candidates(self.registry)],
            ["system", "jemalloc", "mimalloc", "snmalloc"],
        )

    def test_snmalloc_is_arm64_qualified_with_exact_lock_provenance(self) -> None:
        candidate = allocator_registry.candidate_by_id(self.registry, "snmalloc")
        self.assertEqual(candidate["status"], "active")
        self.assertEqual(
            allocator_registry.target_state(candidate, "aarch64-unknown-linux-gnu"),
            "qualified",
        )
        self.assertEqual(
            candidate["lock_packages"],
            [{"name": "snmalloc-sys", "version": "0.7.6"}],
        )

    def test_rpmalloc_remains_x86_64_conditional(self) -> None:
        rpmalloc = allocator_registry.candidate_by_id(self.registry, "rpmalloc")
        self.assertEqual(rpmalloc["status"], "conditional")
        self.assertEqual(
            allocator_registry.target_state(rpmalloc, "aarch64-unknown-linux-gnu"),
            "unsupported",
        )
        self.assertEqual(
            allocator_registry.target_state(rpmalloc, "x86_64-unknown-linux-gnu"),
            "supported-unverified",
        )

    def test_tcmalloc_arm_candidate_is_not_yet_qualified(self) -> None:
        candidate = allocator_registry.candidate_by_id(self.registry, "tcmalloc")
        self.assertEqual(candidate["status"], "planned")
        self.assertEqual(
            allocator_registry.target_state(
                candidate, "aarch64-unknown-linux-gnu"
            ),
            "supported-unverified",
        )

    def test_target_qualification_filters_active_candidates(self) -> None:
        self.assertEqual(
            [
                candidate["identity"]
                for candidate in allocator_registry.qualified_active_candidates(
                    self.registry, "aarch64-unknown-linux-gnu"
                )
            ],
            ["system", "jemalloc", "mimalloc", "snmalloc"],
        )
        self.assertEqual(
            allocator_registry.qualified_active_candidates(
                self.registry, "x86_64-unknown-linux-gnu"
            ),
            (),
        )

    def test_missing_target_state_is_unsupported(self) -> None:
        candidate = allocator_registry.candidate_by_id(self.registry, "system")
        self.assertEqual(
            allocator_registry.target_state(candidate, "x86_64-unknown-linux-gnu"),
            "unsupported",
        )

    def test_duplicate_identity_fails_closed(self) -> None:
        document = copy.deepcopy(self.registry)
        document["candidates"][1]["identity"] = "system"
        with self.assertRaisesRegex(
            allocator_registry.RegistryError, "duplicate allocator identity"
        ):
            allocator_registry.validate_registry(document)

    def test_active_replacement_allocator_requires_lock_provenance(self) -> None:
        document = copy.deepcopy(self.registry)
        document["candidates"][1]["lock_packages"] = []
        with self.assertRaisesRegex(
            allocator_registry.RegistryError,
            "lock_packages must not be empty for active replacement allocator",
        ):
            allocator_registry.validate_registry(document)

    def test_planned_replacement_may_defer_lock_provenance(self) -> None:
        candidate = allocator_registry.candidate_by_id(self.registry, "tcmalloc")
        self.assertEqual(candidate["status"], "planned")
        self.assertEqual(candidate["lock_packages"], [])
        allocator_registry.validate_registry(self.registry)

    def test_active_allocator_requires_qualified_target(self) -> None:
        document = copy.deepcopy(self.registry)
        document["candidates"][2]["target_support"] = {
            "aarch64-unknown-linux-gnu": "supported-unverified"
        }
        with self.assertRaisesRegex(
            allocator_registry.RegistryError, "requires at least one qualified target"
        ):
            allocator_registry.validate_registry(document)

    def test_system_cannot_gain_feature_or_wrapper(self) -> None:
        for field, value in (
            ("cargo_feature", "allocator-system"),
            ("wrapper", {"name": "bad", "version": "1"}),
        ):
            with self.subTest(field=field):
                document = copy.deepcopy(self.registry)
                document["candidates"][0][field] = value
                with self.assertRaises(allocator_registry.RegistryError):
                    allocator_registry.validate_registry(document)


if __name__ == "__main__":
    unittest.main()
