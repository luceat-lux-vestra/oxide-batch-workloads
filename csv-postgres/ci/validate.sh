#!/usr/bin/env bash
set -euo pipefail

workload_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workload_dir"

export DATABASE_URL="${DATABASE_URL:-postgresql://csv_postgres_ci:csv_postgres_ci@localhost:5432/csv_postgres_ci}"
export CSV_POSTGRES_TEST_DATABASE_URL="${CSV_POSTGRES_TEST_DATABASE_URL:-$DATABASE_URL}"

container_name="csv-postgres-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup

docker run -d \
  --name "$container_name" \
  -e POSTGRES_USER=csv_postgres_ci \
  -e POSTGRES_PASSWORD=csv_postgres_ci \
  -e POSTGRES_DB=csv_postgres_ci \
  -p 5432:5432 \
  --health-cmd "pg_isready -U csv_postgres_ci -d csv_postgres_ci" \
  --health-interval 2s \
  --health-timeout 3s \
  --health-retries 20 \
  postgres:18@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280 >/dev/null

status=""
for _ in $(seq 1 60); do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_name")"
  if [[ "$status" == "healthy" ]]; then
    break
  fi
  sleep 1
done

if [[ "$status" != "healthy" ]]; then
  docker logs "$container_name"
  echo "::error::postgres service did not become healthy"
  exit 1
fi

cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked --all-targets

if grep -rnE '\.(unwrap|expect)\(' src/ | grep -v 'fixed calendar date is always valid'; then
  echo "::error::found .unwrap()/.expect() outside the documented exception in src/"
  exit 1
fi

cargo run --locked -q -- migrate
cargo test --locked -- --test-threads=1

set -euo pipefail
cargo run --locked -q -- reset
cargo run --locked -q -- generate --output /tmp/ci_smoke.csv --profile tiny --seed 42
cargo run --locked -q -- run --input /tmp/ci_smoke.csv --import-name ci_smoke --chunk-size 200
cargo run --locked -q -- verify --input /tmp/ci_smoke.csv
