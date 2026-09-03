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