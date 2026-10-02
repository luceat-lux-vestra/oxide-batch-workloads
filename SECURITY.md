# Security Policy

## Scope

This repository contains external-consumer workloads and validation evidence for published OxideBatch releases. A vulnerability in the OxideBatch framework itself should be reported privately to the OxideBatch repository rather than disclosed here as a public workload issue.

Security defects specific to workload code, CI, fixtures, evidence generation, or repository automation are in scope here.

## Reporting

Do not open a public issue for a suspected vulnerability.

Use GitHub Private Vulnerability Reporting (Security → Report a vulnerability) for vulnerabilities specific to this repository. For an OxideBatch framework vulnerability, use the **Security → Advisories → Report a vulnerability** flow in `luceat-lux-vestra/oxide-batch`.

Include the affected workload/commit or OxideBatch release, realistic impact, reproduction steps or proof of concept, and any known mitigation. State whether the issue is already public.

Never include production credentials, personal data, real customer data, or third-party secrets in a report, fixture, log, or evidence file.

## Test data

Validation workloads must use synthetic data. Email-like identifiers should use reserved domains such as `.test`. Credentials committed for disposable local/CI database containers must not be reused outside those isolated environments.

## Failure handling

A failing security, CI, hardening, dependency, or evidence check is an
observation, not a reason to weaken the check. Establish enough root-cause
evidence to justify the owning layer before remediation.

Material `UNKNOWN`, `UNVERIFIED`, and `INSUFFICIENT EVIDENCE` states remain
fail-closed. Deterministic failures are not converted into environment failures
by rerunning them, and premise-changing remediation invalidates affected
exact-HEAD evidence.

The repository does not use a mandatory failure-declaration PR schema,
`failure-triage` merge context, or sticky failure-classification reporter.
