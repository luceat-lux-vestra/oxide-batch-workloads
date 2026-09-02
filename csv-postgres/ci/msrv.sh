#!/usr/bin/env bash
set -euo pipefail

workload_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workload_dir"

cargo build --locked --all-targets
