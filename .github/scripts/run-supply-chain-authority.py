#!/usr/bin/env python3
"""Run the complete repository supply-chain authority in one CI runner.

The discovery projection remains an independent input, but it is never trusted
as the sole completeness authority: it must exactly match the canonical
registry's real workload list before any scans execute.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Callable

VALIDATOR_PATH = Path(__file__).with_name("validate-supply-chain.py")
SPEC = importlib.util.spec_from_file_location("validate_supply_chain", VALIDATOR_PATH)
assert SPEC and SPEC.loader
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)


class SupplyChainAuthorityError(ValueError):
    pass


def parse_discovery_json(payload: str) -> list[str]:
    try:
        matrix = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SupplyChainAuthorityError(f"cannot parse supply-chain discovery JSON: {exc}") from exc

    include = matrix.get("include") if isinstance(matrix, dict) else None
    if not isinstance(include, list) or not include:
        raise SupplyChainAuthorityError("supply-chain discovery must contain a non-empty 'include' array")

    names: list[str] = []
    for entry in include:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            raise SupplyChainAuthorityError("supply-chain discovery contains a malformed workload entry")
        names.append(entry["name"])

    if len(names) != len(set(names)):
        raise SupplyChainAuthorityError("supply-chain discovery contains duplicate workload names")
    return names


def canonical_real_workloads(root: Path) -> list[str]:
    try:
        result = sc.validator.validate_repository(root)
    except sc.validator.RegistryError as exc:
        raise SupplyChainAuthorityError(f"canonical registry validation failed: {exc}") from exc
    names = [entry["name"] for entry in result["workloads"]]
    if not names:
        raise SupplyChainAuthorityError("canonical registry contains zero real workloads")
    return names


def verify_discovery_names(discovered: list[str], canonical: list[str]) -> list[str]:
    if discovered != canonical:
        raise SupplyChainAuthorityError(
            "supply-chain discovery/canonical-registry mismatch: "
            f"discovered={discovered!r}, canonical={canonical!r}"
        )
    return canonical


def verify_discovery_contract(root: Path, payload: str) -> list[str]:
    return verify_discovery_names(
        parse_discovery_json(payload),
        canonical_real_workloads(root),
    )


def run_all(names: list[str], scan: Callable[[str], int]) -> list[str]:
    failures: list[str] = []
    for name in names:
        print(f"::group::supply-chain {name}")
        try:
            exit_code = scan(name)
        except sc.SupplyChainError as exc:
            failures.append(f"{name}: {exc}")
        else:
            if exit_code != 0:
                failures.append(f"{name}: validator exited with code {exit_code}")
        finally:
            print("::endgroup::")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover-json", required=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (defaults to the actual checkout root)",
    )
    parser.add_argument("--cargo-deny-bin", default=sc.DEFAULT_CARGO_DENY_BIN)
    args = parser.parse_args()

    try:
        names = verify_discovery_contract(args.root, args.discover_json)
        failures = run_all(
            names,
            lambda name: sc.run_supply_chain_scan(
                args.root,
                name,
                cargo_deny_bin=args.cargo_deny_bin,
            ),
        )
    except SupplyChainAuthorityError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        raise SystemExit(1)

    print(f"supply-chain authority passed for {len(names)} registered real workload(s)")


if __name__ == "__main__":
    main()
