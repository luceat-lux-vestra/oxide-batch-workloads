# Repository hardening drift audit

The recurring hardening drift audit is a defense-in-depth control for the
repository hardening established under issue #26. It does not define a new
policy layer and it does not replace protected pull-request gates or the
scheduled supply-chain audit.

## Execution and trust boundary

`.github/workflows/hardening-drift-audit.yml` runs weekly, after policy-relevant
changes land on `main`, and may also be started manually with bounded synthetic
lifecycle modes. The `detect` job is read-only and produces a trusted JSON
result artifact. Only the separate `report` job receives `issues: write`, and
that job consumes the detector artifact without executing untrusted
pull-request code.

The detector classifies a run as:

- `clean`: all continuously automatable controls were read and matched;
- `policy-drift`: at least one authoritative control produced a confirmed
  mismatch;
- `infrastructure-failure`: API/readback/tooling failure prevented a complete
  authoritative verdict. Confirmed findings may still be retained in the
  result, but infrastructure failure takes precedence so an incomplete audit
  is never represented as complete.

Detection never remediates repository state.

## Canonical composition

The audit composes existing sources and validators:

| Control family | Canonical source / validator | Recurring audit mode |
| --- | --- | --- |
| workload registration/discovery | `workloads.json`, `validate-workload-registry.py` | automated |
| stable required contexts / main ruleset | `.github/repository-settings-policy.json` + live ruleset API | automated |
| published OxideBatch provenance | `validate-oxidebatch-provenance.py` | automated |
| supply-chain coverage/policy | existing scheduled supply-chain runner and registry-driven validator | automated |
| managed label taxonomy | `.github/labels.json` + live label API | automated |
| label automation security boundary | `label-automation.yml` + `validate-workflow-security.py` | automated static invariant |
| evidence contract / deterministic retention | `validate-evidence.py` | automated |
| immutable action references / critical workflow permissions | repository workflows + `validate-workflow-security.py` | automated static invariant |
| repository/ruleset settings classified `repository-api` or `ruleset-api` | `.github/repository-settings-policy.json` | automated live readback |
| settings classified `manual-readback` | `.github/repository-settings-policy.json` | explicitly reported, not claimed as continuously checked |

The machine-readable settings policy remains authoritative for expected live
values and readback classification. The scheduled audit must not copy those
expected values into workflow YAML.

## Manual-readback controls

Controls whose canonical policy says `manual-readback` are included in every
audit result as an explicit inventory. Their expected state remains policy,
but the low-privilege scheduled workflow does not claim to verify them. No
high-privilege PAT or repository-admin secret is introduced merely to improve
automation coverage.

The first live `main` audit after PR #60, workflow run `33726136040`, provided
an additional permission-boundary proof. Its low-privilege `GITHUB_TOKEN`
repository payload omitted the merge-mode and squash-history fields that had
previously been observed with an admin-scoped #37 readback. Those fields are
therefore classified `manual-readback`; an omitted field is not a confirmed
policy mismatch. For any control still classified as automated, a missing
required API field is treated fail-closed as `infrastructure-failure` rather
than being coerced to `None` and misreported as drift.

## Owned issue lifecycle

Non-clean results are reported through exactly one issue containing:

`<!-- oxide-batch-workloads:hardening-drift-audit -->`

The reporter creates the issue on the first non-clean result, updates it on
repeated failures, reopens it after a regression, and adds a recovery comment
and closes it after a clean run. Multiple issues containing the owned marker
are an error rather than a reason to create another issue.

The generic owned-issue lifecycle is shared with the existing scheduled
supply-chain reporter. Detection remains domain-specific.

The first live audit also exercised this path end-to-end by creating owned
issue #61. That issue is intentionally left to the reporter lifecycle: after
the readback boundary correction lands, the next clean `main` audit should add
the recovery comment and close #61 itself rather than requiring manual cleanup.

## Validation

`test-hardening-drift-audit.py` supplies safe negative fixtures that reach the
audit-level classifier for workload coverage, required contexts, workflow
action/permission security, OxideBatch provenance, supply-chain policy,
managed labels/automation, evidence retention, and live settings. It also
covers infrastructure-vs-policy classification, omitted low-privilege API
fields, explicit manual-readback inventory, and the complete owned-issue
lifecycle.
