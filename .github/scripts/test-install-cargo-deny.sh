#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
installer="$repo_root/.github/scripts/install-cargo-deny.sh"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/cargo-deny-installer-test.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

make_archive() {
  local archive="$1"
  local version="$2"
  local count="$3"
  local payload="$tmp/payload-$RANDOM"
  mkdir -p "$payload"

  local i
  for i in $(seq 1 "$count"); do
    local dir="$payload/bin-$i"
    mkdir -p "$dir"
    cat > "$dir/cargo-deny" <<EOF
#!/usr/bin/env bash
echo "cargo-deny $version"
EOF
    chmod +x "$dir/cargo-deny"
  done

  tar -czf "$archive" -C "$payload" .
}

run_installer() {
  local archive="$1"
  local sha="$2"
  local version="$3"
  local root="$4"
  local path_file="$5"
  RUNNER_TEMP="$tmp" GITHUB_PATH="$path_file"     bash "$installer"       --url "file://$archive"       --sha256 "$sha"       --version "$version"       --archive-name "$(basename "$archive")"       --install-root "$root"       --path-file "$path_file"
}

expect_failure() {
  local name="$1"
  shift
  local log="$tmp/expected-failure-${name// /-}.log"
  if "$@" >"$log" 2>&1; then
    cat "$log" >&2
    echo "::error::$name unexpectedly succeeded" >&2
    exit 1
  fi
  echo "$name: rejected as expected"
}

happy="$tmp/happy.tar.gz"
make_archive "$happy" "0.20.2" 1
happy_sha="$(sha256sum "$happy" | awk '{print $1}')"
path_file="$tmp/github-path"
: > "$path_file"

run_installer "$happy" "$happy_sha" "0.20.2" "$tmp/install-happy" "$path_file"
test -x "$tmp/install-happy/bin/cargo-deny"
test "$("$tmp/install-happy/bin/cargo-deny" --version)" = "cargo-deny 0.20.2"
grep -Fx "$tmp/install-happy/bin" "$path_file" >/dev/null

expect_failure "download failure"   bash "$installer"     --url "file://$tmp/does-not-exist.tar.gz"     --sha256 "$happy_sha"     --version "0.20.2"     --install-root "$tmp/install-missing"     --path-file "$tmp/path-missing"

expect_failure "checksum mismatch"   run_installer "$happy"     "0000000000000000000000000000000000000000000000000000000000000000"     "0.20.2" "$tmp/install-bad-sha" "$tmp/path-bad-sha"

invalid="$tmp/invalid.tar.gz"
printf 'not a gzip archive\n' > "$invalid"
invalid_sha="$(sha256sum "$invalid" | awk '{print $1}')"
expect_failure "extraction failure"   run_installer "$invalid" "$invalid_sha" "0.20.2"     "$tmp/install-invalid" "$tmp/path-invalid"

empty_payload="$tmp/empty-payload"
mkdir -p "$empty_payload"
printf 'fixture\n' > "$empty_payload/README"
no_bin="$tmp/no-bin.tar.gz"
tar -czf "$no_bin" -C "$empty_payload" .
no_bin_sha="$(sha256sum "$no_bin" | awk '{print $1}')"
expect_failure "missing binary"   run_installer "$no_bin" "$no_bin_sha" "0.20.2"     "$tmp/install-no-bin" "$tmp/path-no-bin"

multiple="$tmp/multiple.tar.gz"
make_archive "$multiple" "0.20.2" 2
multiple_sha="$(sha256sum "$multiple" | awk '{print $1}')"
expect_failure "multiple binaries"   run_installer "$multiple" "$multiple_sha" "0.20.2"     "$tmp/install-multiple" "$tmp/path-multiple"

wrong="$tmp/wrong-version.tar.gz"
make_archive "$wrong" "0.20.1" 1
wrong_sha="$(sha256sum "$wrong" | awk '{print $1}')"
expect_failure "version mismatch"   run_installer "$wrong" "$wrong_sha" "0.20.2"     "$tmp/install-wrong-version" "$tmp/path-wrong-version"

expect_failure "malformed digest"   run_installer "$happy" "not-a-sha256" "0.20.2"     "$tmp/install-malformed" "$tmp/path-malformed"

echo "cargo-deny installer failure semantics: PASS"
