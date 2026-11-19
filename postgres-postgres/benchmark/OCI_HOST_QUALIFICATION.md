# OCI allocator host qualification — campaign #323

This is the precondition for claim-bearing allocator comparison on the existing
OCI ARM64 host. It is allocator work only; it does not use payment-reference,
Lima, or campaign #318 infrastructure.

## Frozen host class

The qualification contract currently requires:

- OCI `VM.Standard.A1.Flex`;
- ARM64 / `aarch64-unknown-linux-gnu`;
- at least 4 OCPUs;
- at least 24 GB of OCI shape memory;
- ext4 root filesystem;
- at least 180 GiB root filesystem capacity;
- swap disabled;
- exact published OxideBatch 0.7.0;
- clean detached/exact repository revision;
- all five ARM64-qualified allocators:
  `system`, `jemalloc`, `mimalloc`, `snmalloc`, `tcmalloc`.

The runner reads OCI IMDS v2 directly for instance shape and shape configuration.
It records host/kernel/OS/CPU/filesystem/toolchain/PostgreSQL provenance and then
reuses the existing bounded profile smoke to build and correctness-check every
allocator on the same host.

Qualification timings are diagnostic only. They are not allocator ranking
evidence.

OCI billing/Always-Free status is deliberately outside this evidence contract.
Shape size is not used to infer whether the instance is billed.

## Existing host

The campaign host checkout is expected at:

```text
/opt/oxide-batch-workloads
```

Durable campaign evidence is stored under:

```text
/srv/oxide-batch-workloads
```

Before running qualification, update the checkout to the exact protected-main
revision selected for the campaign. Do not run from a dirty worktree.

## Run

From `postgres-postgres/`:

```bash
sha=<exact-protected-main-40-hex>
out="/srv/oxide-batch-workloads/allocator-campaign/host/qualification-${sha}"

python3 benchmark/oci_host_qualification.py \
  --expected-sha "$sha" \
  --output "$out"
```

The output path must not already exist.

A successful run retains:

```text
qualification-<sha>/
  host.json
  qualification.json
  smoke/
    system/
    jemalloc/
    mimalloc/
    snmalloc/
    tcmalloc/
```

Each allocator directory contains the existing profile-smoke manifest and raw
measurement/verifier evidence.

## Acceptance

Accept the host only when `qualification.json` is `PASS` and independent
inspection confirms:

- OCI metadata matches the frozen contract;
- repository SHA is exact and the checkout was clean;
- PostgreSQL 18 started successfully;
- all five allocator manifests identify campaign #323 and OxideBatch 0.7.0;
- every allocator correctness verdict is `PASS`.

Only after this gate closes should the separate diagnostic allocator matrix run.
