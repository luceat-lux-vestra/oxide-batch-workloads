# Canonical workload evidence contract

This document defines the repository-wide contract for retained workload
evidence. The contract is intentionally narrower than a provenance
attestation system: it makes committed evidence machine-checkable,
recomputable where the repository has enough inputs, squash-merge durable,
and explicit about where trust stops.

The canonical validator is
[`.github/scripts/validate-evidence.py`](scripts/validate-evidence.py).
Workload-specific semantic verification remains workload-owned; the central
validator invokes the verifier declared by each manifest and treats its
canonical `violations` result as authoritative.

## Applicability

`workloads.json` remains the canonical registry. The evidence contract applies
structurally:

- every entry under `workloads` that has a `validation/` directory retains
  evidence and MUST contain `validation/evidence-manifest.json`;
- a real workload with no retained evidence omits `validation/`;
- entries under `fixtures` are CI-orchestration fixtures, make no OxideBatch
  evidence claim, and are outside this contract;
- retained committed JSON evidence lives directly under `validation/` and
  every such JSON file except `evidence-manifest.json` MUST be declared as an
  artifact record in the manifest.

There is no per-workload boolean that can silently disable validation for an
existing `validation/` directory.

## Manifest v2

The top-level object is strict and contains exactly:

```json
{
  "schema_version": 2,
  "workload": "<workloads.json name>",
  "producer": {},
  "semantic_closure": {},
  "validation_subject": {},
  "records": [],
  "verifier": {},
  "environment": {},
  "retention": {},
  "external_artifacts": []
}
```

Unknown top-level fields are rejected so a misspelling cannot silently turn a
required control into decorative metadata.

### Producer revision and squash durability

`producer.base_revision` remains the 40-hex Git commit that identifies the
producer checkout or legacy source snapshot from which the evidence claim was
made. It is provenance metadata and MUST NOT be rewritten to the later squash
commit merely to make validation pass.

`producer.revision_role` is one of:

- `producer-checkout`: the revision was the checked-out source revision used
  for the run;
- `legacy-source-snapshot`: pre-contract evidence did not retain a durable
  pre-run checkout identity. The revision is the historical source snapshot
  used for migration and MUST NOT be represented as a trusted run
  attestation.

`producer.run` records the run kind, an identity when one exists, and a trust
class. Manifest v2 rejects `trusted-producer-bound`: this repository still
has no independently verified external run attestation or cryptographic trust
anchor.

A producer commit can legitimately become unreachable from authoritative
`main` after GitHub squash merge. Full-history checkout cannot recover an
object that is not in `main` history merely because the PR once referenced it.
Therefore v2 does **not** use `producer.base_revision` as the sole historical
content locator. If that commit is available, the validator cross-checks it
against the recorded semantic closure. If it is unavailable, unavailability
alone is not a failure; the exact closure must still satisfy the durable
representation rule below.

The manifest MUST NOT replace the original producer identity with the commit
that first introduces the evidence. A squash commit can act only as a durable
representation of already-recorded semantic content, never as a retroactive
claim that the producer run executed from that squash commit.

### Semantic closure

Evidence output is not source identity. `semantic_closure` identifies only the
inputs that define and produce the claim, such as:

- workload `Cargo.toml` and `Cargo.lock`;
- workload source;
- schema/migrations and relevant runtime configuration;
- the evidence producer script;
- verifier input/implementation that was present at the producer snapshot.

Generated evidence JSON and `evidence-manifest.json` itself are excluded.

Manifest v2 keeps the `sha256-git-tree-entries-v1` serialization and adds the
exact entries used to compute it. Each `entries[]` member records:

```json
{
  "path": "src/example.rs",
  "mode": "100644",
  "git_blob_oid": "<40-hex Git blob object id>"
}
```

The canonical serialization remains:

`<workload-relative-path>\0<git-mode>\0<git-blob-oid>\n`

sorted by workload-relative path and SHA-256 hashed. `entries` must be sorted,
unique, use regular-file Git modes, lie inside the declared `includes`, and
cover every include selector. The recorded `digest_sha256` must equal a fresh
recomputation from those exact entries.

The Git blob object IDs are content identities, not producer-authored verdicts.
The outer SHA-256 gives a deterministic closure identity. Neither identity is
itself a run attestation.

#### Durable representation rule

The complete recorded path/mode/blob entry set MUST be represented exactly by
at least one commit reachable from the checked-out `HEAD`. The validator scans
`HEAD` ancestry and expands the same `includes` selectors at each candidate
commit. A candidate matches only when the **entire** selected entry set is
identical.

This is the squash-stability boundary:

- before merge, an available producer/PR representation can satisfy the rule;
- after squash merge, the authoritative squash commit can satisfy it when the
  semantic producer content was preserved byte-for-byte with the same Git
  modes;
- if squash/rebase/manual integration altered any semantic path, mode, added or
  removed included file, or blob content, no exact representation exists and
  validation fails closed;
- hand-editing the manifest entries and digest without a corresponding exact
  repository representation also fails closed.

When `producer.base_revision` is still available, it must independently expand
to the same exact entry set. Thus PR-time verification cannot silently record
one closure while the named producer commit contains another.

This mechanism deliberately proves repository/content consistency, not
cryptographic authenticity against an actor able to coordinate edits to the
source, evidence, manifest, and repository history. That stronger guarantee
requires an external trust anchor or independently verified attestation.

`excluded_generated_paths` MUST exactly equal the manifest's retained
generated artifact paths. The validator proves none of those paths, nor the
manifest itself, enters the semantic closure.

### Exact validation subject

`validation_subject` binds evidence to the exact published first-party
OxideBatch crates in the semantic producer snapshot.

Manifest v2 reads the exact `Cargo.toml` and `Cargo.lock` blobs recorded in
`semantic_closure.entries`, then reuses the same #29 provenance rules:

- directly declared first-party dependencies are exact `=x.y.z`;
- the lockfile resolves each exactly once from canonical crates.io;
- a valid crates.io checksum is present;
- path/git/workspace/patch/replace/source-replacement escapes are rejected.

The manifest records each direct first-party crate's name, version, registry
source, and checksum plus the producer lockfile Git blob identity. Those fields
must exactly match deterministic recomputation; they are not free-form
producer claims.

Repository/workload `.cargo` source configuration is also checked under the
existing #29 contract. When the original producer commit is unavailable, the
validator uses the durable closure-representation commit as the historical
repository context; current repository-wide #29 validation remains an
independent gate as well.

### Scenario and input identity

Each record contains:

- a unique scenario name;
- one retained artifact path, SHA-256, and byte size;
- an `input.identity` with a stable kind plus at least a SHA-256 and/or stable
  reference;
- workload-specific deterministic reproduction parameters;
- scenario parameters; and
- a deterministic failure point when that scenario injects a failure.

The central validator checks identity shape and artifact integrity. The
workload-owned canonical verifier checks workload-specific relationships such
as dataset parameters, failure boundaries, restart lineage, row/chunk counts,
and final-state equivalence.

A deterministically generated large input is not an "external evidence
artifact" merely because its raw bytes are not committed. Its exact content
digest and deterministic generator parameters identify/recreate the input.
A non-reproducible raw artifact stored elsewhere is an external artifact and
must follow the external-artifact rules below.

### Verifier and canonical verdict

The manifest separately identifies:

- `verifier.producer`: the verifier implementation inside the semantic
  producer closure, by path and Git blob identity; and
- `verifier.canonical`: the current workload-owned retained-evidence verifier,
  by path and SHA-256.

The central validator requires the producer verifier path/OID to equal the
corresponding semantic-closure entry. It then executes the current canonical
verifier. Its machine contract is:

```json
{
  "schema_version": 1,
  "violations": []
}
```

The `violations` array is authoritative. Zero violations plus a zero process
exit code passes; any violation fails.

A producer-authored field such as `passed: true`,
`full_content_digests_match: true`, a display verdict, or a human summary is
never authoritative. The canonical verifier must recompute the relationships
that determine the verdict from retained observations.

### Environment

`environment.observations` records only environment facts meaningful to the
workload. Each observation carries one of the supported trust classes and
names its source. Exact values that were not retained MUST NOT be invented;
`environment.limitations` records those gaps.

### Deterministic retention

`retention.committed_artifacts` defines a deterministic bound:

- direct `validation/` layout;
- maximum retained artifact count;
- maximum total retained artifact bytes; and
- a supersession rule.

CI rejects undeclared retained JSON, excess count, or excess bytes. These
checks are content/layout based and therefore deterministic.

`retention.wall_clock_freshness_merge_gate` MUST be `false`. Ordinary PR
mergeability never becomes impossible merely because evidence aged while no
semantic input changed. Time-based freshness belongs in scheduled audits or a
campaign-specific acceptance requirement.

### External/raw artifacts

`external_artifacts` is empty when all evidence is committed or deterministically
reproducible from declared inputs.

When a non-committed raw artifact is required to interpret a claim, each entry
MUST record:

- content SHA-256;
- a concrete reference;
- the real storage provider/location class; and
- the actual configured retention guarantee.

A reference with no real retention guarantee is invalid metadata for this
purpose. Do not call third-party storage "immutable" unless an enforced
immutability mechanism actually exists. Manifest v2 schema-checks these
declarations; it does not independently audit the external storage provider.

## Trust model

The contract deliberately separates integrity/consistency from authenticity.

| Field/control | Trust class | What v2 proves | What v2 does not prove |
|---|---|---|---|
| Producer/base revision | recorded metadata + conditional Git cross-check | original producer identity is preserved; if available, its closure equals recorded entries | that an unavailable producer commit or manual run was externally attested |
| Semantic closure entries + digest | deterministic recomputation | exact path/mode/blob set is internally consistent and digest-correct | run authenticity against coordinated repository/history edits |
| Durable closure representation | deterministic Git-history check | some `HEAD`-reachable commit contains the complete exact closure, including after squash | that the producer run executed from that durable representation commit |
| OxideBatch subject + lockfile | deterministic recomputation | exact first-party versions/source/checksums match closure `Cargo.toml`/`Cargo.lock` and #29 rules | crates.io/server attestation beyond committed registry checksum semantics |
| Retained artifact digest/size | deterministic recomputation | current committed artifact bytes match the manifest | that the producer did not fabricate those bytes |
| Input identity | schema/internal consistency; workload verifier as applicable | stable identity is present and scenario relationships can be checked | possession/retention of raw bytes unless separately declared |
| Producer verifier | deterministic closure identity | exact verifier blob is part of the producer closure | that it was actually invoked unless a trusted run binding exists |
| Canonical verifier | deterministic current-file identity + execution | verifier bytes match manifest and its `violations` result is executed in CI | correctness of verifier logic beyond review/tests |
| Environment observations | per-entry trust class | declared source/trust semantics are explicit | exact values that were not retained |
| Producer/run identity | recorded metadata in v2 | available identity is preserved | trusted attestation; v2 rejects that stronger claim |
| External artifact metadata | schema/internal consistency | digest/reference/storage/retention fields are present | external storage availability or policy enforcement |

No adjacent digest can detect a coordinated edit if the editor can change both
the content and its recorded digest. Claiming that requires an external trust
anchor, independent replay, or attestation mechanism that v2 does not
implement.

## Existing workload migrations

### `csv-postgres`

The existing `clean-run.json`, `crash-run.json`, and `restart-run.json` predate
this contract. Their v2 migration retains their generated bytes unchanged and
adds the exact historical semantic closure entries. The original
`e0294d62747270d8b4ae959dc1b5f23e27bd9363` remains
`legacy-source-snapshot`; no trusted pre-run attestation is invented.

### `postgres-postgres`

The retained cursor/paging evidence was produced from
`da1273e6e425aae32651ade2b966f52db3af0535`. PR #70 was later squash-merged,
so that producer commit is not guaranteed to remain reachable from
`main`. Manifest v2 preserves the original producer SHA while recording the
exact producer closure entries. The PR #70 squash commit preserves those
semantic blobs and therefore supplies the durable `main` representation
without being relabeled as the producer run revision.

No retained workload evidence JSON is regenerated merely to perform this
schema migration.

## CI and future changes

The `discover` job runs both the evidence-contract tests and canonical
validator before workload fan-out. It uses full Git history because v2 must
search authoritative `HEAD` ancestry for exact semantic-closure
representations and should still cross-check older producer commits when they
remain available. Full history is necessary but is no longer incorrectly
assumed to make pre-squash PR commits durable.

The contract test suite includes an adversarial squash simulation that:

1. creates evidence from a producer commit;
2. creates a squash-style main commit with the resulting content;
3. expires refs/reflogs and prunes the producer commit object;
4. requires validation to pass when all semantic producer entries are
   preserved;
5. requires validation to fail when semantic producer content is altered; and
6. requires hand-edited entries/digests with no exact reachable representation
   to fail.

A new workload-specific evidence shape should add only semantic checks that
belong to that workload verifier. Repository-wide identity, provenance,
retention, squash durability, trust, and canonical-verdict mechanics stay in
the central contract.
