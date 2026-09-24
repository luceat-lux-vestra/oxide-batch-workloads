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

## Repository failure-classification controls

The required `failure-triage` check is an unprivileged `pull_request`
adapter to the organization-wide failure-declaration action, pinned by full
commit SHA. It validates the PR's remediation declaration and remains separate
from automatic failure classification.

The trusted `Failure classification` workflow runs from the default branch
after tracked workflows complete. It rebuilds active failures for the exact PR
HEAD from GitHub Actions metadata and bounded log inspection and upserts one
sticky `CI Failure Classification` comment. It never checks out or executes PR
code or downloaded artifacts. Workflow metadata/log access and PR-comment
mutation remain job-local minimum permissions.

A PR that first introduces this `workflow_run` reporter cannot prove the
reporter against its own pull-request runs because GitHub loads the reporter
from the default branch. Full rollout therefore requires a later PR, after the
reporter is on `main`, whose exact final HEAD passes the ordinary required
checks and receives exactly one sticky classification report for the same HEAD.
When no tracked workflow is pending or failed, that report must reach
`CLEAR`.

`CANDIDATE` and `UNKNOWN` remain fail-closed and never authorize
remediation.

