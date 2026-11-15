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

cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked --all-targets
cargo test --locked
python3 evidence/test_verify_retained_evidence.py

PAYMENT_RECOVERY_EVIDENCE_DIR="$workload_dir/validation" \
  bash ci/recovery-matrix.sh

cp evidence/verify-retained-evidence.py validation/verify-retained-evidence.py

rustc_version="$(rustc --version)"
cargo_version="$(cargo --version)"
postgres_version="$(docker run --rm postgres:18 postgres --version | tr -d '\r')"
python3 evidence/build-manifest.py \
  --validation-dir validation \
  --producer-sha "$GITHUB_SHA" \
  --run-url "$run_url" \
  --rustc "$rustc_version" \
  --cargo "$cargo_version" \
  --postgres "$postgres_version"

python3 validation/verify-retained-evidence.py --manifest validation/evidence-manifest.json
cd "$repo_root"
python3 .github/scripts/validate-evidence.py
