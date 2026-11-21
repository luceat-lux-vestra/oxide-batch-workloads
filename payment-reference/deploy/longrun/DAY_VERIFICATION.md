# Day-independent verification contract v2 (#318)

A terminal campaign day is not allowed to advance the evidence frontier from
generic `PASS + SHA` assertions for settlement or provider side-effect cardinality.

## Version boundary

`oxide-batch-workloads.payment-reference-day-independent-verification` is now
`schema_version: 2`.

Version 1 is intentionally rejected by the current scanner. This is a
backward-incompatible safety boundary: historical or hand-written v1 files
cannot silently gain the stronger meaning of v2.

The top-level v2 record binds:

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

## Remaining checks

Only `reconciliation` and `financial_invariants` temporarily retain the legacy `{verdict, artifact_sha256}` shape. Their dedicated proof contracts remain separate work units.

## Authority boundary

The campaign controller may sequence phases, but v2 frontier scanning derives
settlement, provider-cardinality and recovery PASS from retained independent verifier
artifacts. It does not accept
a controller-authored settlement PASS assertion by itself.

This contract still does not create a terminal day record, execute campaign
traffic, qualify OCI, or advance the frontier on its own.
