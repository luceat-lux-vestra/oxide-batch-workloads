#!/usr/bin/env python3
import json
import os
import shutil
from pathlib import Path
import stat
import subprocess
import tempfile

ASSET_ROOT = Path(__file__).resolve().parent
WORKLOAD_ROOT = ASSET_ROOT.parent.parent
INSTALL = ASSET_ROOT / "install.sh"
REPO_SHA = "0123456789abcdef0123456789abcdef01234567"
SYSTEM_SH = shutil.which("sh")
if SYSTEM_SH is None:
    raise RuntimeError("repository-side S10 asset tests require sh on PATH")
SYSTEM_SH = str(Path(SYSTEM_SH).resolve())
SYSTEM_PS = shutil.which("ps")
if SYSTEM_PS is None:
    raise RuntimeError("repository-side S10 asset tests require ps on PATH")
SYSTEM_PS = str(Path(SYSTEM_PS).resolve())


def write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run(args, *, env=None, expected=0):
    result = subprocess.run(
        [str(value) for value in args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != expected:
        raise AssertionError(
            f"command {args!r} returned {result.returncode}, expected {expected}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def campaign_config(server_root: Path, campaign_id: str) -> Path:
    path = (
        server_root
        / "config"
        / "oxide-batch-workloads"
        / "postgres-postgres-endurance"
        / "campaign.env"
    )
    path.write_text(
        "DATABASE_URL='postgresql://u0_a1510@127.0.0.1/postgres_postgres_workload'\n"
        f"OXIDE_CAMPAIGN_ID='{campaign_id}'\n"
        "OXIDE_SAMPLE_INTERVAL_MS='1000'\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def disk_guard(server_root: Path, body: str) -> Path:
    path = server_root / "scripts" / "disk-guard.sh"
    write_executable(path, f"#!{SYSTEM_SH}\n" + body)
    return path


def test_static_contract() -> None:
    rotation = (ASSET_ROOT / "svlogd.config").read_text(encoding="utf-8").splitlines()
    assert rotation == ["s8388608", "n16"]
    size = int(rotation[0][1:])
    count = int(rotation[1][1:])
    assert size == 8 * 1024 * 1024
    assert count == 16
    assert size * count == 128 * 1024 * 1024

    service_log = (ASSET_ROOT / "service" / "log" / "run").read_text(encoding="utf-8")
    assert 'svlogd -tt "$OXIDE_LOG_ROOT"' in service_log
    assert "OXIDE_EVIDENCE_ROOT" not in service_log

    service_run = (ASSET_ROOT / "service" / "run").read_text(encoding="utf-8")
    assert '"$release_root/bin/device-control" campaign' in service_run

    install_script = INSTALL.read_text(encoding="utf-8")
    assert "--shell-bin" in install_script
    readme = (ASSET_ROOT / "README.md").read_text(encoding="utf-8")
    assert "sh ./deploy/s10/install.sh" in readme

    control = (ASSET_ROOT / "bin" / "device-control").read_text(encoding="utf-8")
    assert '"$sv_bin" once "$service_path"' in control
    assert '"$sv_bin" up "$service_path"' not in control
    assert "clear-stale" in control
    assert "--pre-iteration-guard" in control
    assert "indeterminate:%s" in control
    assert "Operation not permitted" in control
    assert "Permission denied" in control
    assert "process absence is not proven" in control
    assert "BLOCK" in control and "ALLOW" in control

    guarded = (WORKLOAD_ROOT / "src" / "endurance" / "guarded_batch.rs").read_text(
        encoding="utf-8"
    )
    assert ".stdout(Stdio::inherit())" in guarded
    assert ".stderr(Stdio::inherit())" in guarded

    executable_assets = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            ASSET_ROOT / "install.sh",
            ASSET_ROOT / "bin" / "device-control",
            ASSET_ROOT / "service" / "run",
            ASSET_ROOT / "service" / "log" / "run",
        ]
    )
    for forbidden in [
        "postgres-postgres -- reset",
        "TRUNCATE ",
        "DELETE FROM oxide_batch",
        'rm -rf "$OXIDE_EVIDENCE_ROOT"',
    ]:
        assert forbidden not in executable_assets
    assert "192.168." not in executable_assets
    assert "R3CM70F42SH" not in executable_assets


def test_install_guard_lifecycle_and_controller_propagation() -> None:
    with tempfile.TemporaryDirectory(prefix="oxide-s10-deploy-") as temp:
        root = Path(temp)
        server_root = root / "server"
        service_root = root / "services"
        fake_bin = root / "fake"
        workload = fake_bin / "postgres-postgres"
        controller = fake_bin / "postgres-postgres-endurancectl"
        fake_sv = fake_bin / "sv"
        sv_args = root / "sv-args.txt"
        controller_args = root / "controller-args.txt"

        write_executable(workload, f"#!{SYSTEM_SH}\nexit 0\n")
        write_executable(
            controller,
            f"#!{SYSTEM_SH}\n"
            'printf "%s\\n" "$@" > "$FAKE_CONTROLLER_ARGS"\n'
            'printf "planned-child-sigkill-handled\\n"\n'
            'exit "${FAKE_CONTROLLER_EXIT:-0}"\n',
        )
        write_executable(
            fake_sv,
            f"#!{SYSTEM_SH}\n"
            'printf "%s\\n" "$@" > "$FAKE_SV_ARGS"\n'
            'exit "${FAKE_SV_EXIT:-0}"\n',
        )
        disk_guard(server_root, "echo ALLOW\nexit 0\n")

        run(
            [
                SYSTEM_SH,
                INSTALL,
                "--repo-sha",
                REPO_SHA,
                "--workload-bin",
                workload,
                "--controller-bin",
                controller,
                "--server-root",
                server_root,
                "--service-root",
                service_root,
                "--shell-bin",
                SYSTEM_SH,
            ]
        )

        deployment_base = (
            server_root / "workloads" / "oxide-batch-workloads" / "postgres-postgres"
        )
        release = deployment_base / "releases" / REPO_SHA
        current = deployment_base / "current"
        service = service_root / "oxide-batch-postgres-endurance"
        evidence_root = server_root / "evidence" / "oxide-batch-workloads"
        log_root = (
            server_root
            / "logs"
            / "oxide-batch-workloads"
            / "postgres-postgres-endurance"
        )
        state_root = (
            server_root
            / "data"
            / "oxide-batch-workloads"
            / "postgres-postgres-endurance"
        )

        assert current.is_symlink()
        assert current.resolve() == release.resolve()
        assert service.is_symlink()
        assert os.readlink(service) == str(release / "service")
        assert service.resolve() == (release / "service").resolve()
        assert (release / "service" / "down").is_file()
        installed_shell_assets = [
            release / "bin" / "device-control",
            release / "bin" / "pre-iteration-guard",
            release / "service" / "run",
            release / "service" / "log" / "run",
        ]
        for asset in installed_shell_assets:
            first_line = asset.read_text(encoding="utf-8").splitlines()[0]
            assert first_line == f"#!{SYSTEM_SH}"
            assert "/usr/bin/env" not in first_line
        assert list(evidence_root.iterdir()) == []
        assert (log_root / "config").read_text(encoding="utf-8") == "s8388608\nn16\n"

        manifest = json.loads((release / "deployment.json").read_text(encoding="utf-8"))
        assert manifest["schema"] == "oxide-batch-workloads.s10-deployment"
        assert manifest["repository_sha"] == REPO_SHA
        assert manifest["oxide_batch_version"] == "0.6.0"
        assert manifest["oxide_batch_features"] == ["postgres"]
        assert manifest["shell_binary"] == SYSTEM_SH
        assert len(manifest["workload_sha256"]) == 64
        assert len(manifest["controller_sha256"]) == 64

        config = campaign_config(server_root, "devicefixture")
        assert stat.S_IMODE(config.stat().st_mode) == 0o600

        pre_guard = current / "bin" / "pre-iteration-guard"
        allow = run([pre_guard])
        assert "disk-guard allow campaign=devicefixture" in allow.stdout

        disk_guard(server_root, "echo BLOCK\nexit 0\n")
        blocked = run([pre_guard], expected=75)
        assert "disk-guard blocked campaign=devicefixture" in blocked.stderr

        disk_guard(server_root, "echo MAYBE\nexit 0\n")
        unknown = run([pre_guard], expected=70)
        assert "failing closed" in unknown.stderr

        disk_guard(server_root, "echo ALLOW\nexit 23\n")
        failed = run([pre_guard], expected=23)
        assert "disk-guard error campaign=devicefixture exit=23" in failed.stderr

        disk_guard(server_root, "echo ALLOW\nexit 0\n")
        env = os.environ.copy()
        env["FAKE_CONTROLLER_ARGS"] = str(controller_args)
        env["FAKE_CONTROLLER_EXIT"] = "42"
        controller_result = run(
            [current / "bin" / "device-control", "campaign"],
            env=env,
            expected=42,
        )
        assert "planned-child-sigkill-handled" in controller_result.stdout
        assert not (state_root / "controller.pid").exists()

        args = controller_args.read_text(encoding="utf-8").splitlines()
        assert args[0] == "run-campaign"
        assert args[args.index("--evidence-root") + 1] == str(evidence_root)
        assert args[args.index("--campaign-id") + 1] == "devicefixture"
        assert args[args.index("--pre-iteration-guard") + 1] == str(release / "bin" / "pre-iteration-guard")

        env["FAKE_CONTROLLER_EXIT"] = "0"
        run([current / "bin" / "device-control", "campaign"], env=env)

        campaign_config(server_root, "../escape")
        unsafe = run(
            [current / "bin" / "device-control", "campaign"],
            env=env,
            expected=64,
        )
        assert "safe path component" in unsafe.stderr
        campaign_config(server_root, "devicefixture")

        (state_root / "controller.pid").write_text("999999999\n", encoding="utf-8")
        lifecycle_env = os.environ.copy()
        lifecycle_env["OXIDE_SV_BIN"] = str(fake_sv)
        lifecycle_env["FAKE_SV_ARGS"] = str(sv_args)

        refused = run(
            [current / "bin" / "device-control", "start"],
            env=lifecycle_env,
            expected=69,
        )
        assert "clear-stale explicitly" in refused.stderr

        run([current / "bin" / "device-control", "clear-stale"], env=lifecycle_env)
        assert not (state_root / "controller.pid").exists()

        run([current / "bin" / "device-control", "start"], env=lifecycle_env)
        assert sv_args.read_text(encoding="utf-8").splitlines() == [
            "once",
            str(service),
        ]

        status = run(
            [current / "bin" / "device-control", "status"],
            env=lifecycle_env,
        )
        assert "controller_pid_state=none" in status.stdout

        indeterminate_pid = "999999998"
        fake_ps = fake_bin / "ps"
        write_executable(
            fake_ps,
            f"#!{SYSTEM_SH}\n"
            f'if [ "$1" = "-p" ] && [ "$2" = "{indeterminate_pid}" ]; then exit 0; fi\n'
            f'exec "{SYSTEM_PS}" "$@"\n',
        )
        (state_root / "controller.pid").write_text(
            f"{indeterminate_pid}\n",
            encoding="utf-8",
        )
        indeterminate_env = lifecycle_env.copy()
        indeterminate_env["PATH"] = (
            str(fake_bin) + os.pathsep + indeterminate_env.get("PATH", "")
        )

        indeterminate_status = run(
            [current / "bin" / "device-control", "status"],
            env=indeterminate_env,
        )
        assert (
            f"controller_pid_state=indeterminate:{indeterminate_pid}"
            in indeterminate_status.stdout
        )

        indeterminate_start = run(
            [current / "bin" / "device-control", "start"],
            env=indeterminate_env,
            expected=69,
        )
        assert "process ownership is not proven clear" in indeterminate_start.stderr

        indeterminate_clear = run(
            [current / "bin" / "device-control", "clear-stale"],
            env=indeterminate_env,
            expected=69,
        )
        assert "process absence is not proven" in indeterminate_clear.stderr
        assert (state_root / "controller.pid").read_text(encoding="utf-8") == (
            f"{indeterminate_pid}\n"
        )
        (state_root / "controller.pid").unlink()


if __name__ == "__main__":
    test_static_contract()
    test_install_guard_lifecycle_and_controller_propagation()
    print("S10 deployment asset contract: PASS")
