#!/usr/bin/env bash
set -euo pipefail

workload_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_root="$(cd "$workload_dir/.." && pwd)"
cd "$workload_dir"

: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
run_url="https://github.com/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

rm -rf validation
mkdir -p validation
cp evidence/verify-retained-evidence.py validation/verify-retained-evidence.py

cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked --all-targets
python3 evidence/test_verify_retained_evidence.py

docker compose up -d --wait --wait-timeout 60
cleanup() {
  docker compose down --volumes || true
  (cd "$repo_root/payment-reference/deploy" && docker compose -f compose.yaml down --volumes) || true
}
trap cleanup EXIT

target/debug/payment-reconciliation-postgres migrate

PAYMENT_REFERENCE_RESULT_PATH="${RUNNER_TEMP:-/tmp}/payment-reference-scenario-result.json"   "$repo_root/payment-reference/deploy/smoke.sh"

business_date="$(date -u +%F)"
merchant_db="postgresql://merchant:merchant-local-only@127.0.0.1:15435/merchant"
provider_base="http://127.0.0.1:18084"

target/debug/payment-reconciliation-postgres snapshot   --database-url "$DATABASE_URL"   --merchant-database-url "$merchant_db"   --provider-base-url "$provider_base"   --snapshot-id evidence-live-clear   --business-date "$business_date"   --provider-scope merchant-smoke

for spec in   "business-missing-provider:evidence-missing-provider:missing-provider"   "business-missing-internal:evidence-missing-internal:missing-internal"   "business-duplicate-provider:evidence-duplicate-provider:duplicate-provider"   "business-amount-mismatch:evidence-amount-mismatch:amount-mismatch"; do
  IFS=: read -r _scenario snapshot scenario <<<"$spec"
  target/debug/payment-reconciliation-postgres derive-scenario     --database-url "$DATABASE_URL"     --base-snapshot-id evidence-live-clear     --snapshot-id "$snapshot"     --scenario "$scenario"     --delta-minor 1
done

produce_business() {
  scenario="$1"; snapshot="$2"; source_kind="$3"
  target/debug/payment-reconciliation-postgres run --database-url "$DATABASE_URL" --snapshot-id "$snapshot" --chunk-size 2 --page-size 2
  target/debug/payment-reconciliation-postgres finalize --database-url "$DATABASE_URL" --snapshot-id "$snapshot"
  target/debug/payment-reconciliation-postgres verify --database-url "$DATABASE_URL" --snapshot-id "$snapshot" > "${RUNNER_TEMP:-/tmp}/report.json"
  python3 evidence/wrap-business.py --scenario "$scenario" --source-kind "$source_kind"     --report "${RUNNER_TEMP:-/tmp}/report.json" --output "validation/$scenario.json"
}

produce_business business-clear evidence-live-clear payment-reference-live-base
produce_business business-missing-provider evidence-missing-provider payment-reference-derived-snapshot
produce_business business-missing-internal evidence-missing-internal payment-reference-derived-snapshot
produce_business business-duplicate-provider evidence-duplicate-provider payment-reference-derived-snapshot
produce_business business-amount-mismatch evidence-amount-mismatch payment-reference-derived-snapshot

PAYMENT_RECONCILIATION_EVIDENCE_DIR="$workload_dir/validation"   cargo test --locked --test recovery_matrix -- --test-threads=1

rustc_version="$(rustc --version)"
cargo_version="$(cargo --version)"
postgres_version="$(docker compose exec -T postgres postgres --version | tr -d '\r')"
python3 evidence/build-manifest.py   --validation-dir validation   --producer-sha "$GITHUB_SHA"   --run-url "$run_url"   --rustc "$rustc_version"   --cargo "$cargo_version"   --postgres "$postgres_version"

python3 validation/verify-retained-evidence.py --manifest validation/evidence-manifest.json
cd "$repo_root"
python3 .github/scripts/validate-evidence.py
