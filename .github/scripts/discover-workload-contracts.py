#!/usr/bin/env python3

import argparse
import json
from pathlib import Path


class DiscoveryError(ValueError):
    pass


def fail(message: str) -> None:
    raise DiscoveryError(message)


def load_registry(root: Path) -> dict:
    try:
        data = json.loads((root / "workloads.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail("missing canonical workload registry: workloads.json")
    except json.JSONDecodeError as exc:
        fail(f"invalid workloads.json: {exc}")
    if not isinstance(data, dict):
        fail("workloads.json root must be an object")
    workloads = data.get("workloads")
    if not isinstance(workloads, list) or not workloads:
        fail("workloads must be a non-empty array")
    return data


def discover_contracts(root: Path) -> dict[str, object]:
    workloads = load_registry(root)["workloads"]
    ci_entries: list[dict[str, str]] = []
    msrv_entries: list[dict[str, str]] = []
    msrv_not_applicable: list[str] = []

    seen_names: set[str] = set()
    for entry in workloads:
        if not isinstance(entry, dict):
            fail("invalid workload entry in workloads.json")
        name = entry.get("name")
        path = entry.get("path")
        contracts = entry.get("contracts")
        if not isinstance(name, str) or not name:
            fail("invalid workload name in workloads.json")
        if name in seen_names:
            fail(f"duplicate workload name in workloads.json: {name}")
        seen_names.add(name)
        if not isinstance(path, str) or not path:
            fail(f"workload {name!r} has invalid path")
        if not isinstance(contracts, dict):
            fail(f"workload {name!r} has invalid contracts block")

        ci = contracts.get("ci")
        if not isinstance(ci, dict) or not isinstance(ci.get("run"), str) or not ci["run"]:
            fail(f"workload {name!r} has invalid contracts.ci.run")
        ci_entries.append({"name": name, "path": path, "run": ci["run"]})

        msrv = contracts.get("msrv")
        if not isinstance(msrv, dict):
            fail(f"workload {name!r} has invalid contracts.msrv")
        policy = msrv.get("policy")
        if policy == "required":
            toolchain = msrv.get("toolchain")
            run = msrv.get("run")
            if not isinstance(toolchain, str) or not toolchain.strip():
                fail(f"workload {name!r} msrv policy=required is missing toolchain")
            if not isinstance(run, str) or not run:
                fail(f"workload {name!r} msrv policy=required is missing run")
            msrv_entries.append(
                {
                    "name": name,
                    "path": path,
                    "toolchain": toolchain,
                    "run": run,
                }
            )
        elif policy == "not-applicable":
            reason = msrv.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                fail(f"workload {name!r} msrv policy=not-applicable is missing reason")
            msrv_not_applicable.append(name)
        else:
            fail(f"workload {name!r} has unsupported msrv policy: {policy!r}")

    return {
        "ci_matrix": {"include": ci_entries},
        "ci_expected": sorted(entry["name"] for entry in ci_entries),
        "msrv_matrix": {"include": msrv_entries},
        "msrv_expected": sorted(entry["name"] for entry in msrv_entries),
        "msrv_not_applicable": sorted(msrv_not_applicable),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--github-output",
        default=None,
        help="Path to GITHUB_OUTPUT file (defaults to env when omitted in Actions).",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    try:
        discovered = discover_contracts(root)
    except DiscoveryError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc

    print(json.dumps(discovered, indent=2, sort_keys=True))
    output_file = args.github_output
    if output_file:
        path = Path(output_file)
        with path.open("a", encoding="utf-8") as handle:
            for key, value in discovered.items():
                handle.write(f"{key}={json.dumps(value, separators=(',', ':'))}\n")


if __name__ == "__main__":
    main()
