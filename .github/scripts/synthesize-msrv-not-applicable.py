#!/usr/bin/env python3
"""Synthesize explicit MSRV not-applicable results for undeclared entries.

The full canonical discovery remains authoritative. This script only materializes
trusted aggregate evidence for entries whose validated registry policy declares
that no MSRV contract applies. It fails closed on malformed discovery input or
on collisions with downloaded shard results.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

AGGREGATE_PATH = Path(__file__).with_name("aggregate_verdict.py")
AGGREGATE_SPEC = importlib.util.spec_from_file_location("aggregate_verdict", AGGREGATE_PATH)
assert AGGREGATE_SPEC and AGGREGATE_SPEC.loader
aggregate = importlib.util.module_from_spec(AGGREGATE_SPEC)
sys.modules[AGGREGATE_SPEC.name] = aggregate
AGGREGATE_SPEC.loader.exec_module(aggregate)

WRITER_PATH = Path(__file__).with_name("write-shard-result.py")
WRITER_SPEC = importlib.util.spec_from_file_location("write_shard_result", WRITER_PATH)
assert WRITER_SPEC and WRITER_SPEC.loader
writer = importlib.util.module_from_spec(WRITER_SPEC)
WRITER_SPEC.loader.exec_module(writer)


class SynthesisError(ValueError):
    pass


def parse_entries(discover_json: str) -> list[dict]:
    try:
        matrix = json.loads(discover_json)
    except json.JSONDecodeError as exc:
        raise SynthesisError(f"cannot parse discovery JSON: {exc}") from exc

    include = matrix.get("include") if isinstance(matrix, dict) else None
    if not isinstance(include, list) or not include:
        raise SynthesisError("discovery output must contain a non-empty 'include' array")

    entries: list[dict] = []
    seen: set[str] = set()
    for raw in include:
        if not isinstance(raw, dict):
            raise SynthesisError("discovery output contains a non-object entry")
        name = raw.get("name")
        if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
            raise SynthesisError(f"discovery output contains invalid workload name: {name!r}")
        if name in seen:
            raise SynthesisError(f"discovery output contains duplicate workload name: {name!r}")
        seen.add(name)

        msrv = raw.get("msrv")
        if not isinstance(msrv, dict) or not isinstance(msrv.get("declared"), bool):
            raise SynthesisError(f"{name!r} discovery MSRV policy is malformed")

        declared = msrv["declared"]
        version = msrv.get("version")
        reason = msrv.get("policy_reason")
        if declared:
            if not isinstance(version, str) or not version.strip():
                raise SynthesisError(f"{name!r} declared MSRV entry is missing a resolved version")
            if reason not in (None, ""):
                raise SynthesisError(f"{name!r} declared MSRV entry unexpectedly contains a policy reason")
        else:
            if version is not None:
                raise SynthesisError(f"{name!r} undeclared MSRV entry unexpectedly contains a version")
            if not isinstance(reason, str) or not reason.strip():
                raise SynthesisError(f"{name!r} undeclared MSRV entry requires a non-empty policy reason")

        entries.append(raw)
    return entries


def build_not_applicable_result(entry: dict) -> dict:
    msrv = entry["msrv"]
    if msrv["declared"]:
        raise SynthesisError(f"refusing to synthesize not-applicable evidence for declared entry {entry['name']!r}")
    return writer.build_result(
        workload=entry["name"],
        stage="msrv",
        not_applicable=True,
        reason=msrv["policy_reason"],
    )


def synthesize(discover_json: str, results_dir: Path) -> list[Path]:
    entries = parse_entries(discover_json)
    existing, parse_errors = aggregate.load_results(results_dir, "msrv")
    if parse_errors:
        raise SynthesisError("existing MSRV result set is malformed: " + "; ".join(parse_errors))

    existing_names = {result.workload for result in existing}
    written: list[Path] = []
    for entry in entries:
        if entry["msrv"]["declared"]:
            continue
        name = entry["name"]
        if name in existing_names:
            raise SynthesisError(f"refusing to overwrite/collide with existing MSRV result for {name!r}")
        result = build_not_applicable_result(entry)
        output = results_dir / f"synthetic-msrv-{name}" / "result.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result), encoding="utf-8")
        written.append(output)
        existing_names.add(name)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover-json", required=True)
    parser.add_argument("--results-dir", required=True, type=Path)
    args = parser.parse_args()

    try:
        written = synthesize(args.discover_json, args.results_dir)
    except (SynthesisError, ValueError) as exc:
        print(f"::error::{exc}")
        raise SystemExit(1) from exc

    for path in written:
        print(f"synthesized explicit MSRV not-applicable result: {path}")


if __name__ == "__main__":
    main()
