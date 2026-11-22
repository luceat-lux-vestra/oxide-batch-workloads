# Payment-reference fault execution and hard-crash boundary

Owner: #318

This layer executes the deterministic **fault-overlay online lane** for one
logical business day.  It is deliberately not the terminal day controller.

## Preconditions

Execution requires all of the following before any fault-slot mutation:

- exact clean qualified repository source;
- exact host/runtime/image provenance;
- the existing live Compose topology lease;
- the exact day identity written by the online pilot;
- a byte-equivalent completed `normal-traffic.summary.json`;
- the shared Merchant/customer setup journal;
- an explicit loopback fault-proxy endpoint and explicit control timeouts.

An existing incomplete or identity-drifted fault slot blocks the day before any
later slot is mutated.  There is no blind replay of an attempted fault slot.

## Runtime provider identity

The repository-owned Kill Bill adapter derives provider identity as
`kb-<Kill Bill payment UUID>`.  That UUID does not exist when the day plan is
rendered, so a campaign controller must not invent or predict it.

`campaign_fault_catalog.render_runtime_plan()` therefore retains the canonical
`{provider_payment_id}` selector token.  The fault adapter:

1. loads the unresolved catalog plan;
2. arms request steps at logical offset zero;
3. binds the token exactly once from the first matching real provider request;
4. resolves every consumed request/timeline selector to that same identity;
5. rejects timeline dispatch before binding and never rebinds to a different
   payment.

The bound ID is retained in proxy stats and in the fault-slot evidence.  The
webhook-only class has no provider-bound selector, so its provider identity is
read from the resulting Merchant payment view instead.

## Provider-authoritative async failure

The pinned payment-sandbox v1.5.4 public HTTP API has no "fail payment" route.
A proxy-only failed overlay is therefore **not** acceptable end-of-day provider
truth: the independent provider authority reads the authenticated immutable
payment-sandbox admin timeline and requires a real `payment.failed` event.

The payment-sandbox image now contains a non-networked
`payment-sandbox-campaign-control` helper built from the exact same pinned
upstream source revision used by the provider-cardinality authority.  The
runtime payment-sandbox server remains the checksum-verified v1.5.4 release
binary.

For the catalog's asynchronous FAILED transition only, the controller executes
that helper inside the existing payment-sandbox container.  The helper:

- opens the same configured SQLite database;
- requires the existing persisted business virtual time;
- invokes upstream `NewFailPaymentWithPublisher`;
- uses the upstream repository transaction boundary;
- appends the normal `payment.failed` immutable event through the upstream
  event-log repository;
- requires an explicit deterministic correlation ID;
- exposes no HTTP endpoint;
- performs no direct SQL mutation.

The helper intentionally does not become a provider-read authority.  The
existing authenticated admin timeline remains the independent truth checked by
recovery/cardinality.

## Fault-slot journal

Each fault slot retains create-only evidence under the existing day directory:

- exact runtime fault plan;
- write-ahead proxy-control intents and responses;
- write-ahead Merchant order/prepare/authorize intents and responses;
- proxy state immediately after authorize;
- bound provider identity and its binding authority;
- durable provider-failure control intent/response when the catalog requires
  FAILED convergence;
- final proxy state;
- a result that binds source, day-plan digest, fault-catalog digest, provenance,
  provider ID and SHA-256 of every retained per-slot evidence file.

A completed slot may be reused only after all retained file digests and exact
identity fields are revalidated.

The resulting `fault-traffic.summary.json` remains non-accepting.  It does not
claim canonical ordering across the separately executed normal/fault lanes and
sets `day_completion_authorized=false`.

## Retry semantics

This layer never invents a retry policy.

- pre-side-effect 503 remains an online retryable/pending result; recovery may
  replay only the same frozen financial identity after the one-shot plan is
  consumed;
- post-commit response loss remains ambiguous until read reconciliation;
- async pending paths are not mutation-retried while pending;
- async terminal failure is durably represented in payment-sandbox before
  end-of-day provider verification;
- webhook ordering/duplicate/drop/retry is replayed only by the canonical
  catalog schedule.

The catalog reconciliation object is retained in each result for downstream
authority; this executor does not reinterpret it.

## Scheduled hard crashes

`campaign_hard_crash.py` derives the scheduled crash from
`campaign_core.plan_day()` and the reconciliation verifier's canonical
boundary/action mapping.  It does not own crash dates or duplicate the modulo
schedule.

The supervisor starts an explicit argv, waits for the workload-owned semantic
marker, requires the marker PID to equal the exact supervised child, validates
the expected boundary/target, sends SIGKILL(9), reaps that child and proves it
is gone.  Timeout and polling cadence are explicit caller inputs rather than
embedded campaign policy.

The existing recovery and reconciliation verifiers remain responsible for
their full crash-evidence schemas, pre/post database state, new-process
identity, processing generation and publication-count requirements.

## Explicit exclusions

This layer does **not**:

- claim all 100 primary lifecycles completed in one canonical online order;
- run or accept recovery/reconciliation results;
- produce provider cardinality or settlement truth;
- execute Merchant settlement close;
- run quiescence;
- write terminal day PASS;
- advance the campaign frontier;
- make the >=24h / >=30-day campaign acceptance decision.

Those remain later bounded controller/verification work.
