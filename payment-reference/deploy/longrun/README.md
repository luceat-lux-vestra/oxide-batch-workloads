# Payment-reference long-running host qualification

Owner: #318

This directory qualifies the **host and campaign admission boundary**, not the
24-hour campaign itself.

The first service-shaped payment campaign keeps the complete qualified Compose
topology. The canonical first host is now a **shared OCI ARM64 Linux
application host** sized against the current Free-only Always Free floor. The
Galaxy S10 / Termux node remains valid for #203-style native OxideBatch
endurance but is not the deployment architecture for this full Compose
topology.

## Canonical host profile

Profile name:

`oci-free-shared-arm64-linux-v1`

The external service limits were checked on 2026-11-19 against Oracle's
Always Free documentation:

- OCI shape: `VM.Standard.A1.Flex`;
- architecture: ARM64 / aarch64;
- Free-only Always Free A1 budget: 2 OCPU / 12 GB RAM total;
- Always Free Block Volume budget: 200 GB total across boot + block volumes.

Reference:
https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm

The campaign does **not** require one VM per application. A single OCI host is
the intended consolidation boundary.

For a fresh host, the reference storage geometry is:

- 50 GB boot volume;
- 150 GB persistent data block;
- campaign/database/evidence state on the persistent data filesystem.

If the selected OCI host already runs applications, inspect its current disk
and Docker layout first. Do not move an existing Docker data root, repartition,
or recreate the instance merely to match the reference geometry.

The machine-readable qualification floor intentionally tolerates the small
difference between OCI's configured memory and Linux `MemTotal`:

- >= 2 logical CPUs;
- >= 11 GiB observed total memory;
- >= 120 GiB filesystem containing the campaign data/evidence root;
- >= 40 GiB free on that filesystem before qualification/campaign activation.

A smaller host is not qualified.

## Shared-host resource policy

The 12 GB host is budgeted approximately as follows:

| Class | Budget |
| --- | ---: |
| OS + Docker + host agents | 1.5 GiB |
| essential/public applications | 2.5 GiB |
| payment-reference topology + campaign processes | 7 GiB |
| emergency headroom | 1 GiB |

These are planning budgets, not proof that the topology fits.

The host operates in two modes:

- **normal mode**: essential and best-effort applications may run; the
  payment-reference long-run stack is down;
- **campaign mode**: essential applications remain online, best-effort
  applications are paused/stopped when necessary, then the payment-reference
  stack is admitted and started.

Immediately before campaign activation the admission gate requires:

- `MemAvailable >= 7 GiB`;
- already-consumed swap <= 512 MiB;
- campaign data filesystem total/free floors still satisfied;
- ARM64 and >=2 CPU host floors still satisfied.

Run:

```bash
python3 payment-reference/deploy/longrun/check-admission.py \
  --data-path /srv/oxide-batch-workloads
```

Exit 75 / `BLOCK` is a hard stop. Exit 70 means the observation itself could
not be trusted and is also a hard stop.

The campaign has no throughput SLA. CPU contention can make the campaign
slower; OOM, unexpected restart, unresolved swap pressure, or correctness
failure cannot be normalized as success.

## Machine-readable contract

`host-qualification-contract.json` freezes:

- the OCI shared-host profile;
- total host resource floors;
- pre-campaign admission thresholds;
- three consecutive same-volume runtime smokes;
- one explicit payment-reference stack stop/start persistence smoke;
- zero unexpected payment-reference container restarts/OOMs;
- a 30-minute pre-campaign quiescent observation;
- the #203 disk guard policy: at least 20% **and** 20 GiB free.

The repository test is:

```bash
python3 payment-reference/deploy/longrun/test-host-qualification.py
```

It is also invoked by the normal payment-reference runtime smoke so contract
drift fails before expensive runtime setup.

## Build/runtime separation required before OCI activation

The current development/CI Compose path contains repository-owned image builds
for the Kill Bill plugin image, Gateway, Merchant, fault proxy, Kill Bill
database image, and payment-sandbox wrapper.

That is acceptable for CI and development but is **not** the canonical
2-OCPU shared-host long-run path.

Before actual OCI qualification starts, a separate bounded work unit must:

1. build the repository-owned ARM64 runtime images in CI;
2. publish or otherwise retain them by immutable content identity;
3. add a pull-only long-run Compose overlay/path;
4. preserve the existing build-based CI smoke as a separate validation path;
5. prove that the OCI campaign path performs no Maven/Rust image compilation.

Host qualification must not start by forcing a full Maven/Rust Docker build
onto the constrained shared OCI host.

## Qualification sequence

Use an exact clean repository revision. Qualification is run on the selected
shared OCI ARM64 Linux host.

1. Inspect the existing host without mutation:
   - architecture and CPU count;
   - `MemTotal`, `MemAvailable`, swap;
   - data filesystem total/free space;
   - Docker Engine/Compose identity;
   - Docker data root;
   - currently running/co-resident containers and services.
2. Confirm the campaign data/evidence filesystem is persistent and satisfies
   the 120 GiB total / 40 GiB free floors.
3. Enter campaign mode by stopping only explicitly best-effort services.
   Essential/public services remain online.
4. Run `check-admission.py`. A non-ALLOW result blocks activation.
5. Use the pull-only immutable ARM64 campaign image path described above.
   Do not compile repository-owned runtime images on the OCI host.
6. Run:
   ```bash
   docker compose -f payment-reference/deploy/compose.yaml config --quiet
   ```
   together with the selected long-run image overlay once that overlay exists.
7. Run the full runtime smoke three consecutive times against the same running
   Compose project and persistent volumes. Use a unique safe business namespace
   per run. The campaign deployment path must reuse the already-pulled immutable
   images rather than rebuilding them.
8. Capture immutable provenance:
   ```bash
   python3 payment-reference/deploy/longrun/capture-provenance.py \
     --evidence-dir /srv/oxide-batch-workloads/evidence/payment-reference-host-qualification \
     --host-sleep-disabled-attested
   ```
   For OCI, the attestation means the VM is intentionally kept running for the
   qualification/campaign window; it does not assert control of Oracle's
   physical host.
9. Run the disk guard against the durable data/evidence filesystem:
   ```bash
   python3 payment-reference/deploy/longrun/disk-guard.py \
     --path /srv/oxide-batch-workloads
   ```
10. Stop and restart the **payment-reference stack only**, without deleting
    volumes. Run one additional persistence smoke. Whole-VM reboot is not a v1
    qualification requirement for the shared public host.
11. With the complete payment-reference topology running and no business
    mutation/batch work, retain at least 30 minutes of quiescent host/container
    observations.
12. Only after all #318 host gates pass may controller/evidence-schema work and
    the bounded two-logical-day campaign smoke begin.

Do not reduce or remove payment-reference modules merely to fit the host. If
the complete topology cannot pass admission and qualification within the
Free-tier budget, #318 is blocked on current infrastructure.

## Legacy local provisioning assets

`longrun/lima/` contains the previously selected M1 Max/Lima provisioning
path. It is retained temporarily as historical/development infrastructure but
is **not the canonical #318 first-campaign host**.

Do not extend Lima-specific validation or treat successful Lima provisioning as
progress toward the selected OCI qualification path.

## Immutable provenance

`capture-provenance.py` writes exactly four create-only documents:

```text
host.json
runtime.json
container-images.json
toolchains.json
```

No capture timestamp is embedded. If the selected repository/host/runtime/image
identity is unchanged, rerunning capture is a byte-identical replay. If a
document changes, capture fails closed instead of overwriting the first
qualification record.

The capture requires:

- a clean Git worktree and exact 40-hex HEAD;
- ARM64 host and Docker engine architecture;
- resource floors from the frozen contract;
- Compose configuration hash;
- `third-party.lock.json` hash;
- exact running Compose service set, container IDs, restart counts and OOM state;
- actual local Compose image IDs plus architecture/OS/repository digests where
  Docker exposes them;
- guest OS/kernel/memory/swap/filesystem identity;
- Docker/Compose and available host toolchain versions.

The host provenance also records the selected
`oci-free-shared-arm64-linux-v1` profile. The contract hash binds the
pre-campaign admission thresholds even though admission is measured before the
full stack is started.

Local built images may not have a registry `RepoDigest`; their exact Docker
image ID remains mandatory for development evidence. The canonical OCI path
will additionally require immutable published ARM64 image identity.

Provenance capture fails if any expected payment-reference Compose service is
missing/not running, reports an unhealthy state, has a nonzero restart count,
or was OOM-killed.

## Disk guard

`disk-guard.py` uses the same fail-safe threshold as #203:

```text
minimum_free_percent = 20
minimum_free_bytes   = 20 GiB
```

Both conditions must pass. It emits machine-readable JSON followed by an
`ALLOW` or `BLOCK` token. A policy block exits 75; an observation/config
error exits 70.

The guard never deletes evidence, database state, or OxideBatch metadata.

## Persistent-state smoke namespace

`PAYMENT_REFERENCE_SMOKE_NAMESPACE` exists only to make repeated host
qualification runs accumulate independent business identities in the same
persistent databases/provider state. It is restricted to at most 32
characters from `[A-Za-z0-9._-]`.

When unset or empty, all existing CI fixture IDs and retained scenario-result
content remain unchanged. The namespace is appended to Merchant/order/payment,
operation, token/idempotency and fault-matrix provider identities. It is not a
database reset mechanism and must never trigger truncation or volume
recreation.

### Smoke lifecycle flags

Two host-qualification flags currently exist:

- `PAYMENT_REFERENCE_REUSE_TENANT=true` skips tenant creation and relies on
  explicitly reused Kill Bill tenant credentials;
- `PAYMENT_REFERENCE_SKIP_BUILD=true` uses `docker compose up -d` instead
  of `up -d --build`.

Both default to `false`, so ordinary CI retains the original strict build +
HTTP-201 tenant-bootstrap semantics.

For the OCI campaign path, `SKIP_BUILD=true` alone is not sufficient until
the separate pull-only immutable-image overlay is implemented; otherwise the
required images still have to exist locally somehow. That image publication
work is the next infrastructure unit after this contract correction.
