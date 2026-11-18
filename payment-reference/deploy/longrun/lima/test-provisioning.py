#!/usr/bin/env python3
from pathlib import Path

HERE = Path(__file__).resolve().parent
YAML = (HERE / "lima.yaml").read_text(encoding="utf-8")
PROVISION = (HERE / "provision-macos.sh").read_text(encoding="utf-8")

assert 'minimumLimaVersion: 2.3.0' in YAML
assert 'vmType: "vz"' in YAML
assert 'arch: "aarch64"' in YAML
assert 'cpus: 8' in YAML
assert 'memory: "24GiB"' in YAML
assert 'disk: "120GiB"' in YAML
assert 'mounts: []' in YAML
assert 'enabled: false' in YAML and 'binfmt: false' in YAML
assert 'hostLoopback: false' in YAML
assert 'containerd:' in YAML and 'system: false' in YAML and 'user: false' in YAML
assert 'release-20261017/ubuntu-24.04-server-cloudimg-arm64.img' in YAML
assert 'sha256:50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e' in YAML
assert 'usermod -aG docker "{{.User}}"' in YAML
assert '/var/lib/oxide-batch-workloads' in YAML
assert 'docker compose version' in YAML

assert 'LIMA_VERSION="2.3.0"' in PROVISION
assert 'b7af444bd8f91d3c307477e562f2cf3bd7595d62b9429185e4fa841563578cfc' in PROVISION
assert 'Darwin-arm64.tar.gz' in PROVISION
assert 'shasum -a 256' in PROVISION
assert '[ "$(uname -s)" = "Darwin" ]' in PROVISION
assert '[ "$(uname -m)" = "arm64" ]' in PROVISION
assert 'host must provide at least 32 GiB RAM' in PROVISION
assert 'refusing to reuse existing Lima instance' in PROVISION
assert '"$LIMACTL" validate "$lima_yaml"' in PROVISION
assert '"$LIMACTL" --tty=false start --name="$VM_NAME" "$lima_yaml"' in PROVISION
assert '"$LIMACTL" list --quiet "$VM_NAME"' in PROVISION
assert 'git checkout --detach' in PROVISION
assert 'repository SHA mismatch' in PROVISION
assert 'repository checkout is dirty immediately after provisioning' in PROVISION
assert 'The VM is provisioned, but it is NOT yet host-qualified.' in PROVISION

for forbidden in [
    'mount-writable',
    '/Users/',
    'termux',
    'SM-G977',
    'pmset -a',
]:
    assert forbidden not in YAML
    assert forbidden not in PROVISION

assert '""|*[!0-9a-f]*' in PROVISION
assert '[ "${#REPO_SHA}" -eq 40 ]' in PROVISION

print("Lima provisioning asset contract: PASS")
