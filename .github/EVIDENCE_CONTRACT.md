# Canonical workload evidence contract

This document defines the repository-wide contract for retained workload
evidence. The contract is intentionally narrower than a provenance
attestation system: it makes committed evidence machine-checkable,
recomputable where the repository has enough inputs, and explicit about where
trust stops.

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

## Manifest v1

The top-level object is strict and contains exactly:

```json
{
  "schema_version": 1,
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

### Producer/base revision

`producer.base_revision` is a 40-hex Git commit used to recover the source
snapshot from which the manifest's semantic closure is recomputed.

`producer.revision_role` is one of:

- `producer-checkout`: the revision was the checked-out source revision used
  for the run;
- `legacy-source-snapshot`: pre-contract evidence did not retain a durable
  pre-run checkout identity. The revision is the first durable source snapshot
  used for migration and MUST NOT be represented as a trusted run
  attestation.

`producer.run` records the run kind, an identity when one exists, and a trust
class. Manifest v1 intentionally rejects `trusted-producer-bound`: this
repository does not yet verify an external run attestation or cryptographic
trust anchor. A workflow URL, local note, or adjacent digest is recorded
metadata unless a future contract adds an independently verified binding.

The manifest itself MUST NOT use the commit that contains that manifest as a
self-authenticating identity. Existing pre-contract evidence may reference an
older durable source snapshot as `legacy-source-snapshot`; its limitations
remain explicit.

### Semantic closure

Evidence output is not source identity. `semantic_closure` identifies only the
inputs that define and produce the claim, such as:

- workload `Cargo.toml` and `Cargo.lock`;
- workload source;
- schema/migrations and relevant runtime configuration;
- the evidence producer script;
- verifier input/implementation that was present at the producer snapshot.

Generated evidence JSON and `evidence-manifest.json` itself are excluded.

Manifest v1 uses `sha256-git-tree-entries-v1`. The validator:

1. reads the workload tree at `producer.base_revision`;
2. expands each declared `includes` path as an exact file or directory prefix;
3. emits each selected blob as
   `<workload-relative-path>\0<git-mode>\0<git-blob-oid>\n`;
4. sorts by workload-relative path; and
5. SHA-256 hashes the resulting byte stream.

The Git blob object IDs are repository content identifiers used inside the
canonical serialization. The outer SHA-256 is a deterministic closure
identity. This proves internal consistency with the referenced repository
history; it does **not** prove authenticity against a coordinated edit of the
source, manifest, and repository history.

`excluded_generated_paths` MUST exactly equal the manifest's retained
generated artifact paths. The validator also proves none of those paths enters
the semantic closure.

### Exact validation subject

`validation_subject` binds evidence to the exact published first-party
OxideBatch crates used at the producer snapshot.

The validator recovers the producer revision's `Cargo.toml` and `Cargo.lock`
and reuses the same #29 provenance rules:

- directly declared first-party dependencies are exact `=x.y.z`;
- the lockfile resolves each exactly once from canonical crates.io;
- a valid crates.io checksum is present;
- path/git/workspace/patch/replace/source-replacement escapes are rejected.

The manifest records each direct first-party crate's name, version, registry
source, and checksum plus the producer lockfile Git blob identity. Those fields
must exactly match deterministic recomputation; they are not free-form
producer claims.

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

- `verifier.producer`: the verifier implementation at the producer/base
  revision, by path and Git blob identity; and
- `verifier.canonical`: the current workload-owned retained-evidence verifier,
  by path and SHA-256.

The central validator executes the current canonical verifier. Its machine
contract is:

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
workload. Each observation carries one of the trust classes below and names
its source. Exact values that were not retained MUST NOT be invented;
`environment.limitations` records those gaps.

This allows a PostgreSQL workload to record its database image while avoiding
irrelevant broker/object-store fields in workloads that do not use them.

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
immutability mechanism actually exists. Manifest v1 schema-checks these
declarations; it does not independently audit the external storage provider.

## Trust model

The contract deliberately separates integrity/consistency from authenticity.

| Field/control | Trust class | What v1 proves | What v1 does not prove |
|---|---|---|---|
| Producer/base revision | recorded metadata + Git existence | referenced commit exists locally | that a legacy/manual run actually executed from it |
| Semantic closure | deterministic recomputation | selected source/config/schema/producer inputs match the recorded closure digest | authenticity against coordinated repository/history edits |
| OxideBatch subject + lockfile | deterministic recomputation | exact first-party versions/source/checksums match producer `Cargo.toml`/`Cargo.lock` and #29 rules | crates.io/server attestation beyond committed registry checksum semantics |
| Retained artifact digest/size | deterministic recomputation | current committed artifact bytes match the manifest | that the producer did not fabricate those bytes |
| Input identity | schema/internal consistency; workload verifier as applicable | stable identity is present and scenario relationships can be checked | possession/retention of raw bytes unless separately declared |
| Producer verifier | deterministic repository identity | exact verifier blob at producer snapshot | that it was actually invoked unless a trusted run binding exists |
| Canonical verifier | deterministic current-file identity + execution | verifier bytes match manifest and its `violations` result is executed in CI | correctness of the verifier beyond review/tests |
| Environment observations | per-entry trust class | declared source/trust semantics are explicit | exact values that were not retained |
| Producer/run identity | recorded metadata in v1 | available identity is preserved | trusted attestation; v1 rejects that stronger claim |
| External artifact metadata | schema/internal consistency | digest/reference/storage/retention fields are present | external storage availability or policy enforcement |

No adjacent digest can detect a coordinated edit if the attacker/editor can
change both the content and its recorded digest. Claiming that requires an
external trust anchor, independent replay, or attestation mechanism that v1
does not implement.

## `csv-postgres` migration

The existing `clean-run.json`, `crash-run.json`, and `restart-run.json` predate
this contract. Their v1 migration is deliberate:

- their generated bytes are retained unchanged and content-addressed;
- the first durable final source snapshot associated with that regenerated
  evidence, `e0294d62747270d8b4ae959dc1b5f23e27bd9363`, is recorded as
  `legacy-source-snapshot`, not as a trusted pre-run checkout attestation;
- the semantic closure is recomputed from source/config/schema/producer inputs
  in that snapshot and excludes all three generated JSON files;
- the exact historical `oxide-batch` and `oxide-batch-test` 0.6.0 crates and
  lockfile identity are re-derived under #29 provenance rules;
- the canonical retained-evidence verifier recomputes clean/crash/restart
  relationships rather than trusting the generated booleans/notes;
- environment values not retained exactly are called out as limitations.

This migration upgrades machine-checkable provenance without retroactively
inventing producer/run or environment facts.

## CI and future changes

The `discover` job runs both the evidence contract tests and the canonical
validator before workload fan-out. It fetches full Git history because a
manifest can legitimately bind retained evidence to an older producer
revision.

A new workload-specific evidence shape should add only the semantic checks
that belong to that workload verifier. Repository-wide identity, provenance,
retention, trust, and canonical-verdict mechanics stay in the central
contract.

Issue #35 owns the broader mutation/negative-test campaign. Issue #34 keeps
only the load-bearing negative controls required to establish this v1
contract: missing provenance, semantic-closure mismatch, deterministic
retention violation, generated-output exclusion, and proof that a
producer-authored pass flag cannot override a canonical verifier violation.
