# Payment-reference campaign planning and evidence skeleton (#318)

This is the **non-executing controller/evidence-schema slice** for
`payment-reference-longrun-v1`. It consumes no production credentials, makes no
payment mutations, and does **not** run reconciliation, recovery, settlement,
OxideBatch jobs, fault injections, or the 24-hour campaign. It reuses the
append-only and fail-closed frontier concepts proven by the `postgres-postgres`
endurance implementation, without duplicating its workload runner.

## Frozen plan

`campaign-contract-v1.json` fixes the base seed `20261118`, the first logical
business date `2026-11-18` (wall clock does not select it), exactly 100 primary
lifecycle slots per logical day (40 purchase, 25 authorize/capture, 10 void,
15 refund, 10 fault-overlay), and the modulo-10 hard-crash schedule for days
3, 6, 8, and 10. Each normal day requires >=60 s quiescence; every tenth day
requires >=600 s; final quiescence requires >=1800 s. Acceptance still requires
both >=24 h elapsed and >=30 logical business days with >=3,000 primary flows.

A day seed is the unsigned first eight bytes of the SHA-256 of
`payment-reference-longrun-v1:20261118:<sequence>` (big endian). This is a
**new deterministic day-seed encoding**, frozen here before execution. It is
not a statement that upstream provider fault schedules have been executed.

Read-only plan example:

```bash
python3 payment-reference/deploy/longrun/campaign-evidence.py plan --day 10
```

## Campaign identity and create-only evidence

Before `init`, an independently assembled operator input JSON must provide:

- `campaign_id`, exact 40-hex `source_revision`;
- `oxide_batch`: exact `version=0.7.0`, `registry=crates.io`, actual artifact
  SHA-256 and resolved Cargo lockfile SHA-256;
- `batch_binaries`: exact SHA-256 and byte size for `postgres-postgres`,
  `payment-reconciliation-postgres`, `payment-recovery-postgres`;
- `images`: SHA-256 of the immutable OCI manifest and resolved Compose model,
  and all eight service references pinned to GHCR `@sha256:`;
- `provenance_sha256`: four exact host/runtime/container-images/toolchains
  document digests from the qualified host.

`init` verifies the four provenance files against the provided SHA-256 values,
repository revision and image/Compose service identities. It will only create a
**new campaign directory**, never alter a previous one:

```bash
python3 payment-reference/deploy/longrun/campaign-evidence.py init \
  --identity /path/to/independently-verified-campaign-identity.json \
  --provenance-dir /path/to/qualified-host-provenance \
  --evidence-root /path/to/new-campaign-evidence-root
```

The resulting durable layout is:

```text
<campaign-id>/
  campaign.json                # exact frozen source/runtime/lock/image identity
  provenance/                 # four copied create-only host documents
  events.jsonl                # fsynced append-only lifecycle, monotonic ordinal
  days/                       # 000001.json and 000001.verification.json
  statements/                 # reserved for immutable provider statements
  failures/                   # retained diagnostics; presence blocks resume
  samples.jsonl               # reserved for resource samples
```

`status --campaign-dir ...` is **read-only** and returns `READY` only when
consecutive completed-day singletons, terminal event SHA-256, deterministic
plans and independent-verification document shapes/digests agree. It returns
`BLOCK` or exit status 75 on a missing, duplicated, orphaned, malformed,
truncated, out-of-order or nonterminal day, unexpected failure evidence,
provenance drift, or an inconsistent contract. A controller process restart
must call this status boundary before selecting the next day; a started day
with no completed terminal evidence is **not** silently re-run.

The per-day `*.verification.json` format requires PASS entries for recovery,
reconciliation, settlement roster, settlement close, financial invariants,
and external side-effect cardinality, each bound to an artifact SHA-256.
**These entries are only an evidence-envelope schema**: this slice does *not*
verify the underlying external database/provider financial claims. A later
independent verifier and actual controller must produce and validate the
referenced artifacts and enforce ordering before any day can count for the
campaign's acceptance. Synthetic test fixtures exercising the schema are
never campaign evidence.

No CLI for writing completed days is exposed in this slice, so operators
cannot use it to manufacture a completed campaign. There is no `summary.json`
acceptance verifier yet. Changing controller source HEAD, OCI image digests,
OxideBatch dependency/lock provenance or qualified host baseline requires
reviewing and refreshing the **new** campaign identity; never edit old immutable
provenance or existing business evidence in place.

## Deterministic local validation

```bash
python3 payment-reference/deploy/longrun/test-campaign-evidence.py
```

This test is also included in the pre-runtime payment-reference smoke test
set. It covers plans, crash/quiet geometry, create-only provenance, public
registry version pinning, immutable digest references, completed 1/2-day
frontiers, real new-Python-process resume rejection for incomplete days,
evidence tampering, missing independent verification, duplicates, gaps,
truncated JSONL and failure blocking. No OCI container or payment side effect
is needed for this test.
