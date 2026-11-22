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

## Opt-in upgraded OCI host profile

The original `oci-free-shared-arm64-linux-v1` contract remains the default
for the 2 OCPU / 12 GB Always Free planning case. The selected operator OCI VM
was intentionally upgraded and rebooted after the first qualification. For
this **distinct** 4-vCPU / approximately 24-GB guest, select:

```text
payment-reference/deploy/longrun/host-qualification-contract-oci-4vcpu-24gb.json
```

Its observed-memory floor is 23 GiB, CPU floor is 4, and all existing
admission/disk/qualification semantics are otherwise unchanged. This profile
is **not an Always Free entitlement or billing claim**. Verify OCI shape,
tenancy charges and total A1 allocation separately.

Do not rewrite the previous host provenance. Each host/guest change requires a
new evidence directory, with the selected contract SHA bound into the new
create-only provenance. For example, with the complete stack already running
and the immutable OCI `images.env` exported into the shell:

```bash
contract="$PWD/payment-reference/deploy/longrun/host-qualification-contract-oci-4vcpu-24gb.json"
python3 payment-reference/deploy/longrun/check-admission.py \
  --contract "$contract" --data-path /srv/oxide-batch-workloads
python3 payment-reference/deploy/longrun/capture-provenance.py \
  --contract "$contract" \
  --compose-file payment-reference/deploy/longrun/compose.oci.yaml \
  --evidence-dir /srv/oxide-batch-workloads/evidence/<new-host-qualification-id> \
  --host-sleep-disabled-attested
```

The repository contract is an opt-in source of truth for **future** captures;
it does not mutate or retroactively re-sign previously captured host evidence.
A pre-campaign >=30-minute quiescent observation remains mandatory before
activating the controller or long-running campaign.

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

## Immutable ARM64 image publication and OCI pull-only runtime

The development/CI Compose path remains build-based and continues to validate
the repository-owned Dockerfiles.

The canonical OCI campaign path is separate:

- `.github/workflows/payment-reference-images.yml` runs only on protected
  `main` push or explicit workflow dispatch;
- seven ARM64 runtime images are published to GHCR:
  - Kill Bill database image;
  - PostgreSQL 18.6 Bookworm mirror used by Gateway/Merchant databases;
  - payment-sandbox;
  - deterministic fault proxy;
  - Kill Bill + repository-owned plugin;
  - Gateway;
  - Merchant;
- every publication tag is unique to source SHA + workflow run + attempt;
- OCI never deploys a tag. The workflow resolves each pushed image to its
  registry `@sha256:` identity and emits one verified
  `image-manifest.json`;
- the bundle artifact is named
  `payment-reference-arm64-images-<source-sha>` and is retained for 90 days;
- the OCI Compose model requires those digest references and contains no
  `build:` keys.

The PostgreSQL runtime is mirrored into the same GHCR bundle so a mutable
Docker Hub tag is never resolved by the constrained OCI host. The source
`postgres:18.6-bookworm` digest observed by the publication runner is retained
in the per-image manifest entry.

GHCR visibility is not part of the deployment artifact. If the packages are
not public, authenticate Docker on the OCI host with a read-only registry
credential before running the deployment entrypoint. No registry credential is
stored in this repository or in `images.env`.

The OCI deployment entrypoint requires one explicit Kill Bill credential set
to be exported before Compose rendering. The same values are reused by the
qualification smoke; blank values are a configuration error, not a runtime
default:

```bash
export KILLBILL_USERNAME='admin'
export KILLBILL_PASSWORD='password'
export KILLBILL_API_KEY='hostq-318'
export KILLBILL_API_SECRET='hostq-318-local-only'

bash payment-reference/deploy/longrun/deploy-oci.sh \
  --manifest /path/to/image-manifest.json \
  --data-path /srv/oxide-batch-workloads
```

It fail-closes unless:

- all four Kill Bill credential variables are non-blank;
- the manifest source SHA equals the exact clean repository checkout;
- all seven image references are GHCR digest references;
- the resolved OCI Compose model contains exactly the eight expected services
  and zero `build` configuration;
- every service image ends in an exact `@sha256:<64-hex>`;
- the shared-host admission gate passes before image pull;
- the admission gate still passes after the image pull;
- Compose reaches the running/healthy wait boundary without building or
  pulling another image;
- every expected service is still running with restart count 0 and
  `OOMKilled=false`.

The final process start is exactly a no-build/no-pull operation with a bounded
runtime-state wait:

```text
docker compose ... up -d --no-build --pull never --wait --wait-timeout 120
```

The manifest and generated digest-only `images.env` are retained
immutably under:

```text
<durable-data-root>/payment-reference-runtime/
  image-manifest.json
  images.env
```

## Qualification sequence

Use the exact clean repository revision named by the selected image manifest.
Qualification is run on the selected shared OCI ARM64 Linux host.

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
4. Download the `payment-reference-arm64-images-<source-sha>` bundle for the
   exact selected protected-main revision.
5. Authenticate to GHCR when the package visibility requires it.
6. Export one stable local-only Kill Bill credential set for the complete
   qualification window. Do not rely on smoke-script defaults for the initial
   OCI runtime start:

   ```bash
   export KILLBILL_USERNAME='admin'
   export KILLBILL_PASSWORD='password'
   export KILLBILL_API_KEY='hostq-318'
   export KILLBILL_API_SECRET='hostq-318-local-only'
   ```

7. Run `deploy-oci.sh`. It performs admission -> digest-pinned pull ->
   admission -> no-build/no-pull start -> bounded wait -> exact eight-service
   runtime-state verification.
8. Run the full runtime smoke three consecutive times against the same running
   Compose project and persistent volumes. Use the retained OCI Compose/env
   pair and unique safe business namespace per run:

   ```bash
   runtime=/srv/oxide-batch-workloads/payment-reference-runtime

   export PAYMENT_REFERENCE_COMPOSE_FILE="$PWD/payment-reference/deploy/longrun/compose.oci.yaml"
   export PAYMENT_REFERENCE_COMPOSE_ENV_FILE="$runtime/images.env"
   export PAYMENT_REFERENCE_SKIP_BUILD=true
   # KILLBILL_USERNAME/PASSWORD/API_KEY/API_SECRET remain exported from step 6.

   for run in 1 2 3; do
     if [ "$run" = 1 ]; then
       export PAYMENT_REFERENCE_REUSE_TENANT=false
     else
       export PAYMENT_REFERENCE_REUSE_TENANT=true
     fi
     PAYMENT_REFERENCE_SMOKE_NAMESPACE="-hostq-$run" \
       PAYMENT_REFERENCE_RESULT_PATH="$runtime/smoke-$run.json" \
       bash payment-reference/deploy/smoke.sh
   done
   ```

   With `PAYMENT_REFERENCE_SKIP_BUILD=true`, the smoke uses
   `up -d --no-build --pull never`; it cannot compile or fetch an alternative
   image.
9. Capture immutable provenance using the OCI Compose model and its digest env:

   ```bash
   set -a
   . /srv/oxide-batch-workloads/payment-reference-runtime/images.env
   set +a

   python3 payment-reference/deploy/longrun/capture-provenance.py \
     --compose-file payment-reference/deploy/longrun/compose.oci.yaml \
     --evidence-dir /srv/oxide-batch-workloads/evidence/payment-reference-host-qualification \
     --host-sleep-disabled-attested
   ```

   For OCI, the attestation means the VM is intentionally kept running for the
   qualification/campaign window; it does not assert control of Oracle's
   physical host.
10. Run the disk guard against the durable data/evidence filesystem:

   ```bash
   python3 payment-reference/deploy/longrun/disk-guard.py \
     --path /srv/oxide-batch-workloads
   ```

11. Stop and restart the **payment-reference stack only**, without deleting
    volumes, then run one additional persistence smoke with the same OCI
    Compose/env pair and `PAYMENT_REFERENCE_REUSE_TENANT=true`. Whole-VM
    reboot is not a v1 qualification requirement for the shared public host.
12. With the complete payment-reference topology running and no business
    mutation/batch work, retain at least 30 minutes of quiescent host/container
    observations.
13. Only after all #318 host gates pass may controller/evidence-schema work and
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
- `PAYMENT_REFERENCE_SKIP_BUILD=true` uses
  `docker compose up -d --no-build --pull never` instead of
  `up -d --build`;
- `PAYMENT_REFERENCE_COMPOSE_FILE` selects a non-default Compose model;
- `PAYMENT_REFERENCE_COMPOSE_ENV_FILE` supplies the digest-only environment
  generated from the verified image manifest.

All remain opt-in, so ordinary CI keeps the historical build-based Compose
path and strict HTTP-201 tenant-bootstrap semantics. The OCI qualification path
sets all three explicitly and therefore cannot build or pull during a smoke.

## Provider side-effect cardinality authority

The campaign uses the pinned payment-sandbox v1.5.4 authenticated payment
timeline as provider-side effective-mutation truth. See
[PROVIDER_CARDINALITY.md](PROVIDER_CARDINALITY.md). Gateway/Kill Bill journals
and fault-proxy forwarding counters remain separate attempt/harness evidence and
are not promoted to provider financial truth.


## Canonical provider settlement roster

Per-day settlement membership is independently rebuilt from the deterministic
100-slot day plan, Gateway's persisted provider mapping and direct
payment-sandbox provider truth. The controller does not own this list.

See [SETTLEMENT_ROSTER.md](SETTLEMENT_ROSTER.md). The builder emits the existing
`settlement-roster-v1` contract plus an immutable 100-slot verification
sidecar. The existing provider statement producer consumes that roster
unchanged and re-reads provider amounts/version before statement creation.


## Independent settlement statement and Merchant close proof

The provider statement and Merchant settlement close are now designed as one
independent proof stage rather than controller-owned acceptance logic. See
[SETTLEMENT_VERIFICATION.md](SETTLEMENT_VERIFICATION.md).

The verifier consumes the immutable roster + roster-verification pair,
re-produces and immutably retains the provider statement, reconciles prior
Merchant close state from `merchant-db` before mutation, proves exact replay
idempotence, proves digest/totals conflicts are rejected without replacing the
authoritative CLOSED row, and retains a non-accepting deterministic settlement
verification artifact.


## Day-independent verification v5

Terminal-day verification now requires settlement checks to bind the actual
retained independent artifacts rather than generic PASS + SHA claims. See
[DAY_VERIFICATION.md](DAY_VERIFICATION.md).

The v5 scanner verifies campaign/day/source identity, safe relative paths,
exact retained-file hashes, settlement subjects, and the cross-artifact
roster/statement linkage. Versions 1 through 4 day-independent verification are rejected.
Provider side-effect cardinality now also uses a bound retained artifact
rather than generic PASS + SHA. The independent
[`campaign_day_cardinality.py`](campaign_day_cardinality.py) producer requires
all 100 planned provider identities, authentic provider timeline reads,
the pinned cardinality verifier, and the exact independent settlement
roster verification. The scanner replays all 100 raw ledgers and requires
the day proof to cross-bind to the retained settlement roster.

See [DAY_CARDINALITY.md](DAY_CARDINALITY.md).

The frozen fault catalog distinguishes online fault truth from post-recovery
end-of-day provider truth. The pre-side-effect 503 case is online
`PENDING`/absent, then converges under same-identity recovery to EOD
`AUTHORIZED`; cardinality and settlement verification use the EOD contract.
Recovery now also uses a bound retained artifact. `campaign_day_recovery.py` replays the exact #271 secret-safe recovery semantics over the actual day candidate/result state and independently re-reads authenticated payment-sandbox timelines for terminal candidates. The frontier cross-checks those recovery provider proofs against the separately produced day-wide cardinality proof. See [DAY_RECOVERY.md](DAY_RECOVERY.md).

Reconciliation now also uses a bound retained artifact. Reconciliation proof schema v2 freezes a workload-compatible snapshot identity, and `campaign_day_reconciliation.py` now provides a read-only producer/retainer CLI that invokes only the exact campaign-pinned workload `verify` command over already-completed reconciliation state. It replays the retained v0.7.0 oracle, requires CLEAR/one-publication convergence, binds scheduled crash execution evidence, and cross-checks the provider snapshot against the separately retained EOD day-cardinality proof. See [DAY_RECONCILIATION.md](DAY_RECONCILIATION.md).

Financial invariants now also use a bound retained artifact. `campaign_day_financial_invariants.py` replays all 100 retained Merchant/Gateway/Kill Bill/provider EOD snapshots, including qualified fault overlays, and the frontier cross-binds those results to day-cardinality, settlement, reconciliation and recovery proofs. See [DAY_FINANCIAL_INVARIANTS.md](DAY_FINANCIAL_INVARIANTS.md).

Per-day quiescence now has a dedicated read-only resource sampler. `campaign_quiescence.py` derives the required 60/600-second window from the deterministic day plan, observes at a contract target cadence with a five-second maximum accepted gap, binds the exact qualified Compose/source identity, and appends fail-closed host/container/process/PostgreSQL/filesystem evidence to `samples.jsonl`. See [QUIESCENCE.md](QUIESCENCE.md).

Day-wide normal online traffic now has a coordinator over the existing write-ahead one-slot pilot. It safely executes or resumes every non-fault slot while retaining the fault-overlay slots as an explicit external dependency; it cannot mark a day complete. See [ONLINE_TRAFFIC.md](ONLINE_TRAFFIC.md).

The complementary fault-overlay executor now loads the canonical catalog before each provider mutation, binds the real Kill Bill-derived provider identity at runtime, retains fail-closed write-ahead evidence, and durably materializes the catalog's asynchronous provider failure through a non-networked control helper built from the exact pinned payment-sandbox source. Scheduled hard crashes use a separate semantic-marker/SIGKILL supervisor derived from the frozen day plan; neither layer can accept a day. See [FAULT_EXECUTION.md](FAULT_EXECUTION.md).

The real campaign online path now has a separate canonical controller that
interleaves normal and fault slots in the exact deterministic 1..N plan order.
It writes a durable hash-chained order intent/completion journal and refuses to
retroactively promote evidence produced by the lane-first development
coordinators. Completing this journal authorizes the combined online order but
still cannot accept the logical day. See
[ONLINE_CANONICAL.md](ONLINE_CANONICAL.md).

Day verification v5 now also requires independently retained canonical-online
and quiescence proofs. The frontier replays the compact canonical order chain
and re-reads the exact quiescence window from `samples.jsonl`; no
controller-authored generic `PASS + SHA` check remains.
