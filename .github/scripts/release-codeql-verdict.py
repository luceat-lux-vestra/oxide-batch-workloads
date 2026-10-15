#!/usr/bin/env python3
"""Strict original-SARIF verifier for oxide-batch-workloads release-candidate CodeQL evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}\Z")
LANGUAGES = ("actions", "python", "rust", "java-kotlin")
PACK = {"actions": "codeql/actions-queries", "python": "codeql/python-queries", "rust": "codeql/rust-queries", "java-kotlin": "codeql/java-queries"}
REQUIRED = {
    "actions": {
        "actions/untrusted-checkout-toctou/critical",
        "actions/untrusted-checkout-toctou/high",
        "actions/missing-workflow-permissions",
        "actions/cache-poisoning/direct-cache",
        "actions/cache-poisoning/poisonable-step",
        "actions/cache-poisoning/code-injection",
        "actions/artifact-poisoning/critical",
        "actions/untrusted-checkout/high",
        "actions/untrusted-checkout/critical",
        "actions/vulnerable-action",
        "actions/code-injection/critical",
        "actions/improper-access-control",
        "actions/secrets-in-artifacts",
        "actions/excessive-secrets-exposure",
        "actions/unmasked-secret-exposure",
        "actions/envvar-injection/critical",
        "actions/envpath-injection/critical",
    },
    "python": {
        "py/use-of-input",
        "py/sql-injection",
        "py/full-ssrf",
        "py/command-line-injection",
        "py/code-injection",
        "py/unsafe-deserialization",
        "py/ldap-injection",
        "py/xpath-injection",
        "py/xxe",
        "py/template-injection",
        "py/paramiko-missing-host-key-validation",
        "py/path-injection",
        "py/insecure-temporary-file",
        "py/weak-sensitive-data-hashing",
        "py/regex-injection",
        "py/flask-debug",
        "py/csrf-protection-disabled",
    },
    "java-kotlin": {
        "java/sql-injection",
        "java/command-line-injection",
        "java/unsafe-deserialization",
        "java/xss",
        "java/ssrf",
        "java/path-injection",
        "java/zipslip",
        "java/xxe",
        "java/ldap-injection",
        "java/jndi-injection",
        "java/weak-cryptographic-algorithm",
        "java/unsafe-hostname-verification",
        "java/insecure-trustmanager",
        "java/missing-jwt-signature-check",
        "java/spring-disabled-csrf-protection",
        "java/regex-injection",
        "java/unvalidated-url-forward",
    },
    "rust": {
        "rust/insecure-cookie",
        "rust/xss",
        "rust/access-invalid-pointer",
        "rust/cleartext-transmission",
        "rust/weak-cryptographic-algorithm",
        "rust/weak-sensitive-data-hashing",
        "rust/uncontrolled-allocation-size",
        "rust/sql-injection",
        "rust/hard-coded-cryptographic-value",
        "rust/request-forgery",
        "rust/non-https-url",
        "rust/command-line-injection",
        "rust/disabled-certificate-check",
        "rust/path-injection",
        "rust/cleartext-logging",
        "rust/cleartext-storage-database",
        "rust/regex-injection",
    },
}
EXTRACTION = {
    "actions": (
        "actions/diagnostics/successfully-extracted-files",
        "cli/expected-extracted-files/actions",
        None,
    ),
    "python": (
        "py/diagnostics/successfully-extracted-files",
        "cli/expected-extracted-files/python",
        "py/diagnostics/extraction-warnings",
    ),
    "java-kotlin": (
        "java/diagnostics/successfully-extracted-files",
        "cli/expected-extracted-files/java",
        "java/diagnostics/extraction-warnings",
    ),
    "rust": (
        "rust/diagnostics/successfully-extracted-files",
        "cli/expected-extracted-files/rust",
        "rust/diagnostics/extraction-warnings",
    ),
}


def reject(message: str) -> None:
    raise ValueError(message)


def notification_paths(notes: list[dict], notification_id: str) -> set[str]:
    paths: set[str] = set()
    for note in notes:
        if not isinstance(note, dict):
            reject("malformed CodeQL notification")
        descriptor = note.get("descriptor")
        if not isinstance(descriptor, dict) or descriptor.get("id") != notification_id:
            continue
        locations = note.get("locations")
        if not isinstance(locations, list) or not locations:
            reject("extraction diagnostic has no source location")
        for location in locations:
            uri = (
                location.get("physicalLocation", {}).get("artifactLocation", {}).get("uri")
                if isinstance(location, dict)
                else None
            )
            if not isinstance(uri, str) or not uri:
                reject("extraction diagnostic has invalid source URI")
            paths.add(uri)
    return paths


def expected_source(path: str, language: str) -> bool:
    if language == "actions":
        return path.startswith(".github/") and path.endswith((".yml", ".yaml"))
    return path.endswith({
        "python": ".py",
        "java-kotlin": ".java",
        "rust": ".rs",
    }[language])


def coverage(run: dict, language: str) -> tuple[int, int]:
    actual_id, expected_id, warning_id = EXTRACTION[language]
    notes: list[dict] = []
    invocations = run.get("invocations")
    if not isinstance(invocations, list) or not invocations:
        reject("missing CodeQL invocation metadata")
    for invocation in invocations:
        if not isinstance(invocation, dict) or invocation.get("executionSuccessful") is not True:
            reject("CodeQL analysis reports an unsuccessful invocation")
        current = invocation.get("toolExecutionNotifications", [])
        if not isinstance(current, list):
            reject("malformed CodeQL tool notifications")
        notes.extend(current)
    for note in notes:
        descriptor = note.get("descriptor", {}) if isinstance(note, dict) else {}
        nid = descriptor.get("id") if isinstance(descriptor, dict) else None
        if isinstance(note, dict) and note.get("level") == "error":
            reject(f"CodeQL tool execution error: {nid or 'unknown'}")
        if warning_id and nid == warning_id:
            reject("CodeQL extraction warning")
        if nid in {"rust/diagnostics/extraction-errors", "java/diagnostics/extraction-errors",
                   "py/diagnostics/extraction-errors"}:
            reject("CodeQL extraction error")
    actual = notification_paths(notes, actual_id)
    expected = {p for p in notification_paths(notes, expected_id) if expected_source(p, language)}
    if not expected:
        reject("no expected repository source files for CodeQL language")
    if not actual:
        reject("no successfully extracted source files")
    missing = sorted(expected - actual)
    if missing:
        reject(f"CodeQL did not extract {len(missing)} expected source file(s): {', '.join(missing[:8])}")
    return len(actual), len(expected)


def check(directory: Path, language: str, candidate_sha: str) -> dict:
    if language not in LANGUAGES:
        reject("unexpected language")
    if not SHA.fullmatch(candidate_sha):
        reject("candidate must be an exact immutable SHA")
    files = sorted(directory.rglob("*.sarif"))
    if len(files) != 1 or not files[0].is_file() or not files[0].stat().st_size:
        reject(f"expected exactly one nonempty SARIF file, got {len(files)}")
    raw = files[0].read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != "2.1.0":
        reject("not SARIF version 2.1.0")
    runs = data.get("runs")
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        reject("expected exactly one CodeQL SARIF run")
    run = runs[0]
    if run.get("tool", {}).get("driver", {}).get("name") != "CodeQL":
        reject("not CodeQL output")
    if run.get("automationDetails", {}).get("id") not in {
        f"/language:{language}", f"/language:{language}/"
    }:
        reject("SARIF category does not match scanned language")
    extracted, expected = coverage(run, language)

    extensions = run.get("tool", {}).get("extensions")
    if not isinstance(extensions, list):
        reject("missing CodeQL query pack metadata")
    packs = [(i, x) for i, x in enumerate(extensions) if isinstance(x, dict) and x.get("name") == PACK[language]]
    if len(packs) != 1:
        reject("missing or duplicate expected CodeQL query pack")
    component_index, component = packs[0]
    rules = component.get("rules")
    if not isinstance(rules, list) or not rules:
        reject("no CodeQL rule metadata")
    by_id: dict[str, tuple[int, dict]] = {}
    for index, rule in enumerate(rules):
        rid = rule.get("id") if isinstance(rule, dict) else None
        if not isinstance(rid, str) or not rid or rid in by_id:
            reject("malformed or duplicate CodeQL rule ID")
        by_id[rid] = (index, rule)
    missing_rules = sorted(REQUIRED[language] - set(by_id))
    if missing_rules:
        reject(f"{language} security query rule set was reduced: {', '.join(missing_rules)}")

    results = run.get("results")
    if not isinstance(results, list):
        reject("missing or malformed result set")
    high: list[tuple[str, float]] = []
    for result in results:
        rid = result.get("ruleId") if isinstance(result, dict) else None
        if not isinstance(rid, str) or rid not in by_id:
            reject(f"result references unknown rule: {rid}")
        rule_index, rule = by_id[rid]
        reference = result.get("rule")
        component_ref = reference.get("toolComponent") if isinstance(reference, dict) else None
        if (
            not isinstance(reference, dict)
            or reference.get("id") != rid
            or reference.get("index") != rule_index
            or not isinstance(component_ref, dict)
            or component_ref.get("index") != component_index
        ):
            reject(f"result has invalid CodeQL rule reference: {rid}")
        props = rule.get("properties", {})
        rprops = result.get("properties", {})
        if not isinstance(props, dict) or not isinstance(rprops, dict):
            reject("rule/result properties are malformed")
        tags = props.get("tags", [])
        if not isinstance(tags, list):
            reject("rule tags are malformed")
        security = "security" in tags or any(
            isinstance(tag, str) and (tag.startswith("external/cwe/") or tag.startswith("cwe/"))
            for tag in tags
        )
        scores = [obj.get("security-severity") for obj in (props, rprops) if "security-severity" in obj]
        if security and not scores:
            reject(f"security finding has no severity: {rid}")
        for score in scores:
            try:
                value = float(score)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unparseable security severity: {rid}") from exc
            if not math.isfinite(value) or not 0 <= value <= 10:
                reject(f"out-of-range security severity: {rid}")
            if value >= 7.0:
                high.append((rid, value))
    if high:
        reject("High/Critical CodeQL findings: " + ", ".join(f"{rid}({score:g})" for rid, score in high[:8]))
    return {
        "candidate_sha": candidate_sha,
        "language": language,
        "sarif_sha256": hashlib.sha256(raw).hexdigest(),
        "results": len(results),
        "rules": len(rules),
        "required_security_rules": len(REQUIRED[language]),
        "extracted_files": extracted,
        "expected_source_files": expected,
        "high_or_critical": 0,
    }


def fixture(language: str, *, high: bool = False) -> dict:
    required = sorted(REQUIRED[language])
    rules = []
    for rid in required:
        rules.append({"id": rid, "properties": {"tags": ["security"], "security-severity": "8.0" if high and rid == required[0] else "5.0"}})
    source = {
        "actions": ".github/workflows/ci.yml",
        "python": ".github/scripts/validate-evidence.py",
        "rust": "csv-postgres/src/main.rs",
        "java-kotlin": "postgres-postgres/benchmark/java/ci/BenchmarkLauncher.java",
    }[language]
    actual_id, expected_id, _ = EXTRACTION[language]
    return {"version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": "CodeQL"}, "extensions": [{"name": PACK[language], "rules": rules}]},
        "automationDetails": {"id": f"/language:{language}/"},
        "invocations": [{"executionSuccessful": True, "toolExecutionNotifications": [
            {"descriptor": {"id": actual_id}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": source}}}]},
            {"descriptor": {"id": expected_id}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": source}}}]},
        ]}],
        "results": ([] if not high else [{"ruleId": required[0], "rule": {"id": required[0], "index": 0, "toolComponent": {"index": 0}}}]),
    }]}


def self_test() -> None:
    sha = "a" * 40
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "result.sarif"
        for language in LANGUAGES:
            path.write_text(json.dumps(fixture(language)), encoding="utf-8")
            verdict = check(Path(tmp), language, sha)
            assert verdict["required_security_rules"] == 17 and verdict["high_or_critical"] == 0
        cases: list[tuple[dict, str, str]] = []
        high = fixture("rust", high=True)
        cases.append((high, "rust", "High/Critical"))
        missing = fixture("rust")
        missing["runs"][0]["tool"]["extensions"][0]["rules"].pop()
        cases.append((missing, "rust", "rule set was reduced"))
        warning = fixture("rust")
        warning["runs"][0]["invocations"][0]["toolExecutionNotifications"].append({"descriptor": {"id": "rust/diagnostics/extraction-warnings"}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/bad.rs"}}}]})
        cases.append((warning, "rust", "extraction warning"))
        incomplete = fixture("actions")
        incomplete["runs"][0]["invocations"][0]["toolExecutionNotifications"][1]["locations"].append({"physicalLocation": {"artifactLocation": {"uri": ".github/workflows/missing.yml"}}})
        cases.append((incomplete, "actions", "did not extract"))
        for doc, language, message in cases:
            path.write_text(json.dumps(doc), encoding="utf-8")
            try:
                check(Path(tmp), language, sha)
            except ValueError as exc:
                assert message in str(exc), str(exc)
            else:
                raise AssertionError(f"fail-open SARIF case: {message}")
        path.write_text("{bad", encoding="utf-8")
        try:
            check(Path(tmp), "rust", sha)
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError("malformed SARIF accepted")
    print("release CodeQL SARIF negative controls passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--sarif-dir", type=Path)
    parser.add_argument("--language")
    parser.add_argument("--candidate-sha")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.sarif_dir or not args.language or not args.candidate_sha:
        parser.error("--sarif-dir, --language and --candidate-sha are required")
    try:
        verdict = check(args.sarif_dir, args.language, args.candidate_sha)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"::error::Release CodeQL evidence rejected: {exc}", file=sys.stderr)
        return 1
    print("Release CodeQL evidence accepted: " + json.dumps(verdict, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
