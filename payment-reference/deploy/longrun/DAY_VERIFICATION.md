# Day-independent verification contract v4 (#318)

A terminal campaign day is not allowed to advance the evidence frontier from
generic `PASS + SHA` assertions for settlement or provider side-effect cardinality.

## Version boundary

`oxide-batch-workloads.payment-reference-day-independent-verification` is now
`schema_version: 4`.

Versions 1 through 3 are intentionally rejected by the current scanner. This is a
backward-incompatible safety boundary: historical or hand-written files cannot
silently gain the complete bound-proof meaning of v4.

The top-level v4 record binds:

- `campaign_id`;
- `logical_day_sequence`;
- exact campaign `source_revision`;
- overall `PASS` verdict;
- exactly six independent check records.

## Settlement artifact descriptors

`settlement_roster` and `settlement_close` no longer accept only
`{verdict, artifact_sha256}`.

Each descriptor must bind:

- `verdict = PASS`;
- exact artifact schema and schema version;
- campaign ID;
- logical day sequence;
- source revision;
- safe campaign-relative retained artifact path;
- SHA-256 of the exact retained file bytes;
- a subject identity derived again from the retained artifact.

The expected retained filenames are day-scoped:

- `statements/day-NNNNNN.roster.verification.json`;
- `statements/day-NNNNNN.settlement.verification.json`.

Absolute paths, traversal, backslash paths, symlink components, missing files,
wrong day filenames, and file-SHA mismatches fail closed.

## Roster subject

The roster check subject binds:

- canonical roster digest;
- provider account;
- settlement date;
- currency;
- eligible payment count.

The retained artifact must be
`payment-reference-settlement-roster-verification/v1` with decision
`SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED`, the exact campaign/day/source
identity, the expected artifact field set, and an actual 100-slot observation
envelope.

## Settlement-close subject

The settlement-close check subject binds:

- canonical roster digest;
- statement ID and statement digest;
- provider account;
- settlement date;
- currency;
- statement line count;
- captured/refunded/net totals.

The retained artifact must be
`payment-reference-settlement-verification/v1` with decision
`SETTLEMENT_VERIFIED_NOT_DAY_ACCEPTED`, the exact campaign/day/source
identity, the exact statement-summary field set, and the complete Merchant
close proof. The scanner requires the persisted CLOSED row to equal the
statement identity/totals, one close event, replay 200, both conflict controls
409, and unchanged authoritative state.

The scanner additionally requires:

- the close artifact's `roster_verification_sha256` to equal the compact
  canonical digest of the exact retained roster-verification artifact;
- roster digest, provider account, settlement date and currency to agree across
  both settlement artifacts;
- statement line count to equal the roster's eligible payment count.

This prevents mixing valid artifacts from different rosters or logical days.

## Provider day-cardinality subject

`side_effect_cardinality` now uses the same bound artifact descriptor,
pointing to `statements/day-NNNNNN.provider-cardinality.verification.json`.

The scanner requires exactly 100 distinct planned Merchant-to-Provider
mappings, replays every retained provider timeline through the pinned
v1.5.4 provider ledger verifier, and checks exact mutation counts, terminal
financial states, absent-provider HTTP 404 evidence, and the immutable
roster verification digest. The bound subject contains slot count, total
effective provider mutations, roster/roster-verification digests, and
the pinned provider source revision.

See [DAY_CARDINALITY.md](DAY_CARDINALITY.md).

## Recovery subject

`recovery` now binds `days/day-NNNNNN.recovery.verification.json` instead of accepting a generic PASS + SHA assertion. The recovery producer replays the secret-safe #271 candidate/result semantics, checks deterministic day membership and the exact recovery binary identity, and independently re-reads each terminal candidate's authenticated provider timeline. The frontier then cross-checks those per-candidate provider proofs against the separately produced 100-slot day-cardinality artifact. Scheduled day-8-style recovery crashes additionally require retained SIGKILL/new-process execution evidence.

See [DAY_RECOVERY.md](DAY_RECOVERY.md).

## Reconciliation subject

`reconciliation` now binds `days/day-NNNNNN.reconciliation.verification.json` instead of accepting a generic PASS + SHA assertion. Reconciliation proof schema v2 uses the workload-compatible `<campaign-id>-day-NNNNNN-reconciliation` snapshot identity. Its read-only producer verifies the exact campaign-pinned binary, invokes only the workload `verify` command against already-completed state, then independently replays the exact v0.7.0 oracle before create-only retention. The proof requires a terminal CLEAR publication, exact binary/verifier identities, and scheduled crash execution evidence for qualified reconciliation crash boundaries. The frontier then cross-checks the retained provider snapshot against the separately produced end-of-day provider-cardinality proof.

See [DAY_RECONCILIATION.md](DAY_RECONCILIATION.md).

## Financial-invariants subject

`financial_invariants` now binds `days/day-NNNNNN.financial-invariants.verification.json` instead of accepting a generic PASS + SHA assertion. The retained proof replays all 100 Merchant/Gateway/Kill Bill/provider EOD snapshots against the deterministic day plan, including the five qualified fault-overlay classes, then the frontier cross-binds provider state to day-cardinality, financial totals to settlement, stable Merchant/provider identity pairs to reconciliation, and recovered terminal states to recovery evidence.

See [DAY_FINANCIAL_INVARIANTS.md](DAY_FINANCIAL_INVARIANTS.md).

## Remaining checks

There are no remaining legacy `{verdict, artifact_sha256}` day checks in v4. All six checks require bound retained artifacts.

## Authority boundary

The campaign controller may sequence phases, but v4 frontier scanning derives
settlement, provider-cardinality, recovery, reconciliation and financial-invariant
PASS from retained independent verifier artifacts. It does not accept
a controller-authored settlement PASS assertion by itself.

This contract still does not create a terminal day record, execute campaign
traffic, qualify OCI, or advance the frontier on its own.
