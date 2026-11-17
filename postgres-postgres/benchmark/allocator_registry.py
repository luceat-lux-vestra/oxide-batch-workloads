#!/usr/bin/env python3
"""Canonical allocator-candidate registry for campaign #266."""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any

SCHEMA = "oxide-batch-allocator-registry/v1"
DEFAULT_PATH = Path(__file__).with_name("allocator_registry.json")
STATUSES = {"active", "conditional", "planned", "retired"}
SCREENING_TIERS = {"baseline", "phase0", "phase1", "conditional"}
TARGET_STATES = {"qualified", "supported-unverified", "unsupported"}
TELEMETRY_STATES = {"not-wired", "available-not-wired", "wired"}


class RegistryError(ValueError):
    """Raised when allocator registry data violates the campaign contract."""


def fail(message: str) -> None:
    raise RegistryError(message)


def require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{field} must be an object")
    return value


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        fail(f"{field} must be an array")
    return value


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"{field} must be a non-empty string")
    return value


def validate_registry(document: Any) -> dict[str, Any]:
    registry = require_dict(document, "registry")
    if registry.get("schema") != SCHEMA:
        fail(f"schema must be '{SCHEMA}'")

    candidates = require_list(registry.get("candidates"), "candidates")
    if not candidates:
        fail("candidates must not be empty")

    identities: set[str] = set()
    features: set[str] = set()
    telemetry_features: set[str] = set()
    system_count = 0

    for index, raw_candidate in enumerate(candidates):
        field = f"candidates[{index}]"
        candidate = require_dict(raw_candidate, field)
        identity = require_string(candidate.get("identity"), f"{field}.identity")
        if identity in identities:
            fail(f"duplicate allocator identity: {identity}")
        identities.add(identity)

        status = candidate.get("status")
        if status not in STATUSES:
            fail(f"{field}.status must be one of {sorted(STATUSES)}")
        tier = candidate.get("screening_tier")
        if tier not in SCREENING_TIERS:
            fail(f"{field}.screening_tier must be one of {sorted(SCREENING_TIERS)}")

        feature = candidate.get("cargo_feature")
        wrapper = candidate.get("wrapper")
        lock_packages = require_list(candidate.get("lock_packages"), f"{field}.lock_packages")

        if identity == "system":
            system_count += 1
            if feature is not None:
                fail("system allocator cargo_feature must be null")
            if wrapper is not None:
                fail("system allocator wrapper must be null")
            if lock_packages:
                fail("system allocator lock_packages must be empty")
            if tier != "baseline":
                fail("system allocator screening_tier must be baseline")
        else:
            feature = require_string(feature, f"{field}.cargo_feature")
            if feature in features:
                fail(f"duplicate allocator cargo feature: {feature}")
            features.add(feature)
            wrapper_obj = require_dict(wrapper, f"{field}.wrapper")
            require_string(wrapper_obj.get("name"), f"{field}.wrapper.name")
            require_string(wrapper_obj.get("version"), f"{field}.wrapper.version")
            if status == "active" and not lock_packages:
                fail(
                    f"{field}.lock_packages must not be empty for active replacement allocator"
                )

        seen_packages: set[str] = set()
        for package_index, raw_package in enumerate(lock_packages):
            package_field = f"{field}.lock_packages[{package_index}]"
            package = require_dict(raw_package, package_field)
            name = require_string(package.get("name"), f"{package_field}.name")
            require_string(package.get("version"), f"{package_field}.version")
            if name in seen_packages:
                fail(f"{field} contains duplicate lock package {name}")
            seen_packages.add(name)

        upstream = require_dict(candidate.get("upstream"), f"{field}.upstream")
        upstream_status = upstream.get("status")
        if upstream_status not in {"observed", "unavailable"}:
            fail(f"{field}.upstream.status must be observed or unavailable")
        if upstream_status == "observed":
            require_string(upstream.get("version"), f"{field}.upstream.version")
            if upstream.get("reason") is not None:
                fail(f"{field}.upstream.reason must be absent when observed")
        else:
            if upstream.get("version") is not None:
                fail(f"{field}.upstream.version must be null when unavailable")
            require_string(upstream.get("reason"), f"{field}.upstream.reason")

        target_support = require_dict(candidate.get("target_support"), f"{field}.target_support")
        if not target_support:
            fail(f"{field}.target_support must not be empty")
        qualified = False
        for target, state in target_support.items():
            require_string(target, f"{field}.target_support target")
            if state not in TARGET_STATES:
                fail(f"{field}.target_support[{target!r}] must be one of {sorted(TARGET_STATES)}")
            qualified = qualified or state == "qualified"
        if status == "active" and not qualified:
            fail(f"{field} active allocator requires at least one qualified target")

        prerequisites = require_list(
            candidate.get("build_prerequisites"), f"{field}.build_prerequisites"
        )
        for prerequisite_index, prerequisite in enumerate(prerequisites):
            require_string(
                prerequisite,
                f"{field}.build_prerequisites[{prerequisite_index}]",
            )

        telemetry = require_dict(candidate.get("telemetry"), f"{field}.telemetry")
        telemetry_status = telemetry.get("status")
        if telemetry_status not in TELEMETRY_STATES:
            fail(f"{field}.telemetry.status must be one of {sorted(TELEMETRY_STATES)}")
        capabilities = require_list(
            telemetry.get("capabilities"), f"{field}.telemetry.capabilities"
        )
        for capability_index, capability in enumerate(capabilities):
            require_string(
                capability,
                f"{field}.telemetry.capabilities[{capability_index}]",
            )

        telemetry_feature = telemetry.get("cargo_feature")
        if telemetry_status == "wired":
            telemetry_feature = require_string(
                telemetry_feature, f"{field}.telemetry.cargo_feature"
            )
            if not capabilities:
                fail(f"{field}.telemetry.capabilities must not be empty when wired")
            if telemetry_feature == feature:
                fail(f"{field}.telemetry.cargo_feature must be separate from benchmark cargo_feature")
            if telemetry_feature in telemetry_features:
                fail(f"duplicate allocator telemetry cargo feature: {telemetry_feature}")
            telemetry_features.add(telemetry_feature)
        elif telemetry_feature is not None:
            fail(f"{field}.telemetry.cargo_feature is valid only when telemetry is wired")

    if system_count != 1:
        fail("registry must contain exactly one system allocator")
    system = next(candidate for candidate in candidates if candidate["identity"] == "system")
    if system["status"] != "active":
        fail("system allocator must remain active")

    return registry


def load_registry(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"could not load allocator registry: {exc}") from exc
    return validate_registry(document)


def active_candidates(registry: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(
        candidate
        for candidate in registry["candidates"]
        if candidate["status"] == "active"
    )


def qualified_active_candidates(
    registry: dict[str, Any], target: str
) -> tuple[dict[str, Any], ...]:
    return tuple(
        candidate
        for candidate in active_candidates(registry)
        if candidate["target_support"].get(target) == "qualified"
    )


def target_state(candidate: dict[str, Any], target: str) -> str:
    return candidate["target_support"].get(target, "unsupported")


def candidate_by_id(registry: dict[str, Any], identity: str) -> dict[str, Any]:
    for candidate in registry["candidates"]:
        if candidate["identity"] == identity:
            return candidate
    fail(f"unknown allocator: {identity}")


def replacement_features(registry: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        candidate["cargo_feature"]
        for candidate in active_candidates(registry)
        if candidate["cargo_feature"] is not None
    )


def wired_telemetry_candidates(
    registry: dict[str, Any], target: str | None = None
) -> tuple[dict[str, Any], ...]:
    candidates = active_candidates(registry)
    if target is not None:
        candidates = qualified_active_candidates(registry, target)
    return tuple(
        candidate
        for candidate in candidates
        if candidate["telemetry"]["status"] == "wired"
    )


def telemetry_feature(candidate: dict[str, Any]) -> str | None:
    if candidate["telemetry"]["status"] != "wired":
        return None
    return candidate["telemetry"]["cargo_feature"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--active-lines", action="store_true")
    mode.add_argument("--replacement-pairs-lines", action="store_true")
    mode.add_argument("--telemetry-lines", action="store_true")
    parser.add_argument("--target")
    args = parser.parse_args()

    registry = load_registry()
    selected = active_candidates(registry)
    if args.target:
        selected = qualified_active_candidates(registry, args.target)

    if args.active_lines:
        if args.target and not selected:
            fail(f"no active allocators are qualified for target {args.target}")
        for candidate in selected:
            feature = candidate["cargo_feature"] or "-"
            print(f"{candidate['identity']}|{feature}")
        return 0

    if args.replacement_pairs_lines:
        features = tuple(
            candidate["cargo_feature"]
            for candidate in selected
            if candidate["cargo_feature"] is not None
        )
        for left, right in itertools.combinations(features, 2):
            print(f"{left}|{right}")
        return 0

    if args.telemetry_lines:
        telemetry_candidates = wired_telemetry_candidates(registry, args.target)
        if args.target and not telemetry_candidates:
            fail(f"no wired allocator telemetry is qualified for target {args.target}")
        for candidate in telemetry_candidates:
            print(f"{candidate['identity']}|{telemetry_feature(candidate)}")
        return 0

    print(
        json.dumps(
            {
                "valid": True,
                "schema": registry["schema"],
                "active": [candidate["identity"] for candidate in active_candidates(registry)],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
