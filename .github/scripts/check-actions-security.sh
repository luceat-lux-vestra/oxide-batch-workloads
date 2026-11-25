#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOLS_DIR="${RUNNER_TEMP:-$(mktemp -d)}/oxide-batch-workloads-actions-security"
mkdir -p "$TOOLS_DIR"

# Native gomod Dependabot owns actionlint. The maintained install-action
# supplies the checksum-verified zizmor executable.
(
  cd "$ROOT/.github/tools/actionlint"
  go build -mod=mod -o "$TOOLS_DIR/actionlint" github.com/rhysd/actionlint/cmd/actionlint
)
"$TOOLS_DIR/actionlint" -version
zizmor --version

# The current actionlint release does not yet parse GitHub's documented `$/...` self-repository
# syntax for reusable workflow calls (rhysd/actionlint#711). Keep the canonical
# GitHub syntax and suppress only that known parser diagnostic until upstream ships support.
"$TOOLS_DIR/actionlint" \
  -ignore 'reusable workflow call "\$/\.github/workflows/.+" at "uses" is not following the format' \
  "$ROOT"/.github/workflows/*.yml
zizmor --no-online-audits --format plain "$ROOT"/.github/workflows/

fixture_dir="$TOOLS_DIR/negative/.github/workflows"
mkdir -p "$fixture_dir"
cat > "$fixture_dir/unsafe.yml" <<'YAML'
name: Deliberately Unsafe
on: push
permissions: write-all
jobs:
  unsafe:
    runs-on: ubuntu-nonexistent-runner
    steps:
      - uses: actions/checkout@v4
YAML

actionlint_status=0
actionlint_output="$("$TOOLS_DIR/actionlint" -no-color "$fixture_dir/unsafe.yml" 2>&1)" || actionlint_status=$?
if [ "$actionlint_status" -eq 0 ] || ! grep -q 'runner-label' <<<"$actionlint_output"; then
  echo "actionlint negative control did not prove the expected finding" >&2
  printf '%s\n' "$actionlint_output" >&2
  exit 1
fi

zizmor_status=0
zizmor_output="$(zizmor --no-online-audits --format plain --no-progress "$fixture_dir" 2>&1)" || zizmor_status=$?
if [ "$zizmor_status" -eq 0 ] || ! grep -q 'unpinned-uses' <<<"$zizmor_output"; then
  echo "zizmor negative control did not prove the expected finding" >&2
  printf '%s\n' "$zizmor_output" >&2
  exit 1
fi

echo "workflow semantic/security scanners: PASS"
