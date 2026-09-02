#!/usr/bin/env python3
"""Canonical, unit-testable aggregate verdict logic for the workload CI fan-out.

The central workflow's aggregate jobs (`workloads-ci`, `workloads-msrv`) call
this module's CLI. All decision logic lives in `compute_verdict`, a pure
function over plain data, so it can be exercised by
`test_aggregate_verdict.py` without any GitHub Actions runtime.

Fail-closed contract: aggregate success requires an exact, complete,
duplicate-free set of matching per-shard results for every workload the
canonical registry/discovery step produced, with every upstream stage
(discovery, and the fan-out job as a whole) itself reporting success.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

VALID_STATUSES = {"success", "failure"}
VALID_OUTCOMES = {"validated", "not-applicable"}


@dataclass
class ShardResult:
    workload: str
    stage: str
    status: str
    outcome: str
    source: str  # artifact filename or synthetic identifier, for diagnostics


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)


def _parse_result_file(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name}: unreadable/invalid result file ({exc})"
    if not isinstance(data, dict):
        return None, f"{path.name}: result file root must be an object"
    return data, None


def load_results(results_dir: Path, stage: str) -> tuple[list[ShardResult], list[str]]:
    """Load every `*.json` shard result file in results_dir for the given stage.

    Returns (results, parse_errors). A malformed file is reported as a parse
    error rather than silently skipped -- fail-closed applies to diagnostics
    too, not only to the pass/fail verdict.
    """
    results: list[ShardResult] = []
    errors: list[str] = []
    if not results_dir.is_dir():
        return results, errors
    for path in sorted(results_dir.rglob("*.json")):
        data, error = _parse_result_file(path)
        if error is not None:
            errors.append(error)
            continue
        missing_keys = {"workload", "stage", "status", "outcome"} - set(data)
        if missing_keys:
            errors.append(f"{path.name}: result missing key(s): {', '.join(sorted(missing_keys))}")
            continue
        if data["stage"] != stage:
            continue
        if data["status"] not in VALID_STATUSES or data["outcome"] not in VALID_OUTCOMES:
            errors.append(f"{path.name}: result has invalid status/outcome: {data['status']!r}/{data['outcome']!r}")
            continue
        results.append(
            ShardResult(
                workload=data["workload"],
                stage=data["stage"],
                status=data["status"],
                outcome=data["outcome"],
                source=path.name,
            )
        )
    return results, errors


def compute_verdict(
    expected: list[str],
    results: list[ShardResult],
    *,
    discovery_ok: bool = True,
    upstream_job_ok: bool = True,
    parse_errors: list[str] | None = None,
    job_statuses: dict[str, str] | None = None,
) -> Verdict:
    """Compute the aggregate pass/fail verdict and human-readable reasons.

    expected: canonical workload names from registry discovery for this stage.
    results: parsed per-shard result records (may contain duplicates/extras;
        that is exactly what this function must detect).
    discovery_ok: whether the registry/discovery step itself succeeded.
    upstream_job_ok: whether the fan-out job as a whole reported success at
        the GitHub Actions job level (coarse defense-in-depth signal).
    parse_errors: unreadable/malformed result files, reported verbatim.
    job_statuses: optional {workload: conclusion} from the Actions Jobs API,
        used only to sharpen diagnostics for missing workloads (e.g.
        "cancelled" vs "skipped" vs "no matching job found"); never required
        for correctness.
    """
    verdict = Verdict(ok=True)

    if not discovery_ok:
        verdict.fail("registry/discovery step did not succeed; aggregate cannot trust the expected workload set")

    if not expected:
        verdict.fail("zero-workload state: canonical discovery produced no expected workloads")
        # Nothing further can be meaningfully checked against an empty
        # expected set; still fall through so extra/duplicate results (if
        # any) are reported too.

    expected_set = set(expected)
    if len(expected_set) != len(expected):
        verdict.fail("internal error: expected workload list contains duplicates")

    for error in parse_errors or []:
        verdict.fail(f"malformed result: {error}")

    by_workload: dict[str, list[ShardResult]] = {}
    for result in results:
        by_workload.setdefault(result.workload, []).append(result)

    for workload, entries in sorted(by_workload.items()):
        if len(entries) > 1:
            sources = ", ".join(e.source for e in entries)
            verdict.fail(f"duplicate result(s) for workload {workload!r}: {sources}")
        if workload not in expected_set:
            sources = ", ".join(e.source for e in entries)
            verdict.fail(f"unexpected extra result for unregistered/renamed workload {workload!r}: {sources}")

    for workload in sorted(expected_set):
        entries = by_workload.get(workload, [])
        if not entries:
            status = (job_statuses or {}).get(workload)
            if status == "cancelled":
                verdict.fail(f"missing result for workload {workload!r}: shard job was cancelled")
            elif status == "skipped":
                verdict.fail(f"missing result for workload {workload!r}: shard job was unexpectedly skipped")
            elif status is not None:
                verdict.fail(f"missing result for workload {workload!r}: shard job conclusion was {status!r}")
            else:
                verdict.fail(f"missing result for workload {workload!r}: no shard result found")
            continue
        entry = entries[0]
        if entry.status != "success":
            verdict.fail(f"workload {workload!r} shard reported failure ({entry.stage})")

    if not upstream_job_ok:
        verdict.fail("fan-out job did not report success at the GitHub Actions job level")

    return verdict


def _load_expected(discover_json: str) -> list[str]:
    try:
        matrix = json.loads(discover_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"::error::cannot parse discovery output as JSON: {exc}")
    include = matrix.get("include") if isinstance(matrix, dict) else None
    if not isinstance(include, list):
        raise SystemExit("::error::discovery output missing an 'include' array")
    names = []
    for entry in include:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise SystemExit("::error::discovery output contains a malformed workload entry")
        names.append(entry["name"])
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=["ci", "msrv"])
    parser.add_argument("--discover-json", required=True, help="raw JSON emitted by discover-workloads.py")
    parser.add_argument("--results-dir", required=True, type=Path)
    parser.add_argument("--discovery-ok", required=True, choices=["true", "false"])
    parser.add_argument("--upstream-job-ok", required=True, choices=["true", "false"])
    parser.add_argument("--job-statuses-json", default="{}", help='optional {"workload": "conclusion"} JSON')
    args = parser.parse_args()

    expected = _load_expected(args.discover_json)
    results, parse_errors = load_results(args.results_dir, args.stage)
    try:
        job_statuses = json.loads(args.job_statuses_json)
    except json.JSONDecodeError:
        job_statuses = {}

    verdict = compute_verdict(
        expected,
        results,
        discovery_ok=args.discovery_ok == "true",
        upstream_job_ok=args.upstream_job_ok == "true",
        parse_errors=parse_errors,
        job_statuses=job_statuses if isinstance(job_statuses, dict) else {},
    )

    print(f"stage: {args.stage}")
    print(f"expected workloads ({len(expected)}): {', '.join(sorted(expected)) or '(none)'}")
    print(f"observed results ({len(results)}): {', '.join(sorted(r.workload for r in results)) or '(none)'}")
    if verdict.ok:
        print("verdict: PASS - complete matching result set, all shards succeeded")
    else:
        print("verdict: FAIL")
        for reason in verdict.reasons:
            print(f"::error::{reason}")
    raise SystemExit(0 if verdict.ok else 1)


if __name__ == "__main__":
    main()
