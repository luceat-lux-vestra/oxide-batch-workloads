#!/usr/bin/env python3
"""#318 one-normal-slot Merchant pilot, with fsynced write-ahead intent.

NOT the campaign runner: never auto-replays an ambiguous or previously
attempted financial mutation; never advances a logical-day frontier.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SOURCE_RE = re.compile(r"[0-9a-f]{40}\Z")
NORMAL = {
    "auth_capture_purchase": ("PAID",),
    "authorize_capture": ("AUTHORIZED", "PARTIALLY_CAPTURED", "PAID"),
    "authorize_cancel": ("AUTHORIZED", "CANCELLED"),
    "auth_capture_refund": ("PAID", "PARTIALLY_REFUNDED", "REFUNDED"),
}
PROVENANCE_FILES = ("host.json", "runtime.json", "container-images.json", "toolchains.json")
COMPOSE_SERVICES = {"merchant", "gateway", "killbill", "payment-sandbox", "payment-sandbox-fault-proxy", "merchant-db", "gateway-db", "killbill-db"}
BASE = "http://127.0.0.1:18083"


def load_traffic():
    spec = importlib.util.spec_from_file_location("payment_campaign_traffic", HERE / "campaign-traffic-plan.py")
    if spec is None or spec.loader is None:
        raise ValueError("cannot load day traffic contract")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


traffic = load_traffic()


class Block(ValueError):
    pass


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise Block(reason)


def stable(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive_write(path: Path, value: Any) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "wb") as file:
        file.write(stable(value))
        file.flush()
        os.fsync(file.fileno())
    fsync_dir(path.parent)


def read_object(path: Path) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"missing/unsafe evidence: {path.name}")
    value = json.loads(path.read_bytes())
    require(type(value) is dict, f"invalid JSON object: {path.name}")
    return value


def qualified_source(expected_sha: str, provenance_dir: Path) -> dict[str, str]:
    require(isinstance(expected_sha, str) and SOURCE_RE.fullmatch(expected_sha) is not None, "invalid expected source SHA")
    actual = subprocess.run(
        ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.strip()
    require(actual == expected_sha, "Git HEAD differs from requested source SHA")
    dirty = subprocess.run(
        ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), "status", "--porcelain"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    require(not dirty, "source worktree is dirty")
    hashes: dict[str, str] = {}
    for name in PROVENANCE_FILES:
        path = provenance_dir / name
        value = read_object(path)
        require(value.get("repository_sha") == expected_sha, f"{name}: source/provenance mismatch")
        hashes[name] = sha(path.read_bytes())
    host = read_object(provenance_dir / "host.json")
    require(host.get("host_profile") == "oci-shared-arm64-linux-4vcpu-24gb-v1", "upgraded OCI host profile required")
    runtime = read_object(provenance_dir / "runtime.json")
    require(isinstance(runtime.get("compose_services"), list) and set(runtime["compose_services"]) == COMPOSE_SERVICES and len(runtime["compose_services"]) == len(COMPOSE_SERVICES), "exact eight-service Compose provenance required")
    return hashes


def attest_live_topology(provenance_dir: Path) -> dict[str, str]:
    """Read-only Docker identity lease; reject a stale or mixed Compose runtime."""
    record = read_object(provenance_dir / "container-images.json")
    raw = record.get("images")
    require(type(raw) is list and len(raw) == len(COMPOSE_SERVICES), "exact image provenance set required")
    expected: dict[str, dict[str, Any]] = {}
    for entry in raw:
        require(type(entry) is dict and entry.get("service") in COMPOSE_SERVICES, "invalid provenance service")
        service = entry["service"]
        require(service not in expected, "duplicate provenance service")
        require(isinstance(entry.get("container_id"), str) and entry["container_id"], "missing provenance container id")
        require(isinstance(entry.get("image_id"), str) and entry["image_id"].startswith("sha256:"), "missing provenance image id")
        image_ref = entry.get("image_ref")
        require(isinstance(image_ref, str) and re.search(r"@sha256:[0-9a-f]{64}\Z", image_ref) is not None, "mutable provenance image reference")
        expected[service] = entry
    require(set(expected) == COMPOSE_SERVICES, "image provenance service set mismatch")

    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=oxide-payment-reference"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.split()
    require(len(ids) == len(COMPOSE_SERVICES) and len(set(ids)) == len(ids), "wrong Docker project container count")
    result = subprocess.run(
        ["docker", "inspect", *ids],
        capture_output=True, text=True, timeout=15, check=True,
    )
    observed = json.loads(result.stdout)
    require(isinstance(observed, list) and len(observed) == len(COMPOSE_SERVICES), "invalid Docker inspection set")
    identities: dict[str, str] = {}
    health_required = {"payment-sandbox", "merchant-db", "gateway-db", "killbill-db"}
    for container in observed:
        require(isinstance(container, dict), "invalid Docker inspection record")
        config = container.get("Config")
        require(type(config) is dict, "invalid Docker Config")
        labels = config.get("Labels")
        require(type(labels) is dict and labels.get("com.docker.compose.project") == "oxide-payment-reference", "foreign Docker project")
        service = labels.get("com.docker.compose.service")
        require(service in expected and service not in identities, "unexpected or duplicate live service")
        state = container.get("State")
        require(type(state) is dict and state.get("Running") is True, f"{service}: not running")
        require(state.get("OOMKilled") is False and type(container.get("RestartCount")) is int and container["RestartCount"] == 0, f"{service}: restarted or OOM")
        health = state.get("Health")
        require(health is None or type(health) is dict, f"{service}: malformed health")
        status = health.get("Status") if health is not None else None
        require(status == "healthy" if service in health_required else status in (None, "healthy"), f"{service}: unhealthy")
        source = expected[service]
        require(container.get("Id") == source["container_id"] and container.get("Image") == source["image_id"], f"{service}: live container/image drift")
        identities[service] = container["Id"]
    require(set(identities) == COMPOSE_SERVICES, "missing live Compose service")
    return identities


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        raise Block("redirect is not a qualified Merchant mutation outcome")


def http_post(base: str, path: str, body: dict[str, Any]) -> tuple[int, bytes]:
    require(base == BASE, "only exact Merchant loopback endpoint may be used")
    require(path.startswith("/") and "//" not in path and "?" not in path and ".." not in path, "unsafe HTTP path")
    request = urllib.request.Request(
        base + path,
        data=stable(body),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        data = response.read(1_000_001)
        require(len(data) <= 1_000_000, "HTTP response exceeds 1MB")
        return int(response.status), data


def http_get(base: str, path: str) -> dict[str, Any]:
    require(base == BASE, "only exact Merchant loopback endpoint may be used")
    require(path.startswith("/payments/") and re.fullmatch(r"/payments/[A-Za-z0-9_-]{1,120}", path) is not None, "unsafe payment GET path")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(base + path, method="GET"), timeout=10) as response:
        require(response.status == 200, "Merchant GET not successful")
        data = response.read(1_000_001)
        require(len(data) <= 1_000_000, "Merchant GET too large")
    value = json.loads(data)
    require(type(value) is dict, "Merchant GET must return object")
    return value


def step_files(folder: Path, ordinal: int) -> tuple[Path, Path]:
    return folder / f"{ordinal:02d}-intent.json", folder / f"{ordinal:02d}-response.json"


def record_post(
    folder: Path, ordinal: int, request: dict[str, Any], expected_status: int,
    expected_state: str | None, post: Callable[[str, str, dict[str, Any]], tuple[int, bytes]],
) -> dict[str, Any] | None:
    intent, result = step_files(folder, ordinal)
    require(not intent.exists() and not result.exists(), "previous request attempt detected; never send again")
    require(request.get("method") == "POST" and type(request.get("body")) is dict, "invalid Merchant request")
    payload = request["body"]
    # Commit the precise request identity before any network mutation.
    exclusive_write(intent, {
        "schema": "payment-reference-pilot-intent-v1", "ordinal": ordinal,
        "method": "POST", "path": request["path"], "body_sha256": sha(stable(payload)),
        "expected_status": expected_status,
    })
    try:
        status, body = post(BASE, request["path"], payload)
    except Exception as exc:
        # An intent without a result is ambiguous even if the socket failed.
        raise Block("transport outcome unknown; intent retained; manual reconciliation required") from exc
    parsed: dict[str, Any] | None
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    exclusive_write(result, {
        "schema": "payment-reference-pilot-response-v1", "ordinal": ordinal,
        "status": status, "body_sha256": sha(body), "json_body": parsed,
    })
    require(status == expected_status, f"HTTP {status}, expected {expected_status}; no retry")
    if expected_state is not None:
        require(type(parsed) is dict, "missing payment operation view")
        require(parsed.get("paymentState") == expected_state, "Merchant operation state mismatch")
        require(parsed.get("operationState") == "SUCCEEDED", "nonterminal Merchant operation")
        require(parsed.get("operationId") == payload.get("operationId"), "Merchant operation identity mismatch")
        require(parsed.get("merchantPaymentId") == request["path"].split("/")[2], "Merchant payment identity mismatch")
    return parsed


def validate_setup(folder: Path, setup: list[dict[str, Any]]) -> bool:
    """A setup with *all* acknowledged 201s may be reused for another slot."""
    if not folder.exists():
        return False
    require(folder.is_dir() and not folder.is_symlink(), "unsafe setup directory")
    for i, req in enumerate(setup, 1):
        intent, response = step_files(folder, i)
        require(intent.exists() and response.exists(), "ambiguous/incomplete Merchant day setup; manual reconciliation")
        item = read_object(intent)
        ack = read_object(response)
        require(
            item.get("ordinal") == i
            and item.get("method") == "POST"
            and item.get("path") == req["path"]
            and item.get("body_sha256") == sha(stable(req["body"]))
            and item.get("expected_status") == 201,
            "setup request identity drift",
        )
        require(ack.get("ordinal") == i and ack.get("status") == 201, "setup did not finish with HTTP 201")
    require(set(folder.iterdir()) == {
        path for i in range(1, len(setup) + 1) for path in step_files(folder, i)
    }, "unexpected setup evidence")
    return True


def run_one(
    *, plan: dict[str, Any], slot: int, evidence_root: Path, source_sha: str,
    provenance_hashes: dict[str, str],
    post: Callable[[str, str, dict[str, Any]], tuple[int, bytes]] = http_post,
    get: Callable[[str, str], dict[str, Any]] = http_get,
) -> dict[str, Any]:
    require(type(slot) is int and 1 <= slot <= 100, "slot outside 1..100")
    item = plan["slots"][slot - 1]
    require(item["slot"] == slot and item["kind"] in NORMAL, "fault overlay is not eligible for normal pilot")
    require(not plan["executable"] and plan["external_verification_required"], "planner claim boundary changed")
    root = evidence_root.resolve()
    require(root.exists() and root.is_dir() and not root.is_symlink(), "operator evidence root must exist")
    day = root / f'{plan["campaign_id"]}-day{plan["logical_day_sequence"]:06}'
    identity = {
        "schema": "payment-reference-pilot-day-v1",
        "campaign_id": plan["campaign_id"],
        "logical_day_sequence": plan["logical_day_sequence"],
        "source_sha": source_sha,
        "day_plan_sha256": sha(stable(plan)),
        "provenance_sha256": provenance_hashes,
    }
    try:
        day.mkdir(mode=0o700)
        first = True
    except FileExistsError:
        first = False
    require(day.is_dir() and not day.is_symlink(), "unsafe pilot evidence day directory")
    with (day / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if first:
            exclusive_write(day / "identity.json", identity)
        else:
            require(read_object(day / "identity.json") == identity, "pilot day source/provenance/plan drift")
        setup_dir = day / "setup"
        setup = plan["merchant_setup"]
        if not setup_dir.exists():
            setup_dir.mkdir(mode=0o700)
            fsync_dir(day)
            for i, request in enumerate(setup, 1):
                record_post(setup_dir, i, request, 201, None, post)
        require(validate_setup(setup_dir, setup), "day setup not validated")
        item_dir = day / f"slot-{slot:03}"
        # A previous attempted slot is NEVER automatically replayed, even
        # when its last response was durably observed.
        item_dir.mkdir(mode=0o700, exist_ok=False)
        fsync_dir(day)
        requests = [item["create_order"], item["prepare_payment"], *item["mutations"]]
        for i, request in enumerate(requests, 1):
            state = NORMAL[item["kind"]][i - 3] if i >= 3 else None
            observed = record_post(item_dir, i, request, 201 if i <= 2 else 200, state, post)
            if i == 2:
                require(
                    type(observed) is dict
                    and observed.get("merchantPaymentId") == item["merchant_payment_id"]
                    and observed.get("merchantOrderId") == item["merchant_order_id"],
                    "prepared payment identity mismatch",
                )
        # A GET does not replace the independent provider+Kill Bill verifier.
        view = get(BASE, f'/payments/{item["merchant_payment_id"]}')
        require(view.get("merchantPaymentId") == item["merchant_payment_id"], "final Merchant GET payment identity drift")
        require(view.get("merchantOrderId") == item["merchant_order_id"], "final Merchant GET order identity drift")
        require(view.get("paymentState") == item["expected_normal_terminal_state"], "final Merchant GET state mismatch")
        require(view.get("amountMinor") == 1000 and view.get("currency") == "USD", "final Merchant GET amount mismatch")
        result = {
            "decision": "ONE_SLOT_EXECUTED_NOT_FINANCIALLY_VERIFIED",
            "day": plan["logical_day_sequence"],
            "slot": slot,
            "kind": item["kind"],
            "merchant_payment_id": item["merchant_payment_id"],
            "merchant_order_id": item["merchant_order_id"],
            "requests": len(requests),
            "independent_financial_verification_required": True,
        }
        exclusive_write(item_dir / "result.json", result)
        return result


def main() -> int:
    p = argparse.ArgumentParser(description="#318 normal one-slot pilot, not campaign completion")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--execute-one", action="store_true")
    p.add_argument("--expected-source-sha")
    p.add_argument("--provenance-dir", type=Path)
    p.add_argument("--evidence-root", type=Path)
    args = p.parse_args()
    try:
        plan = traffic.build(args.campaign_id, args.day)
        require(1 <= args.slot <= 100, "invalid slot")
        selected = plan["slots"][args.slot - 1]
        require(selected["kind"] in NORMAL, "fault-overlay slot cannot run without fault-engine verification")
        if not args.execute_one:
            print(json.dumps({"decision": "PLAN_ONLY", "slot": selected, "no_http_mutations": True}, sort_keys=True))
            return 0
        require(args.provenance_dir is not None and args.evidence_root is not None, "execution requires provenance and evidence root")
        require(args.expected_source_sha is not None, "execution requires exact expected source SHA")
        hashes = qualified_source(args.expected_source_sha, args.provenance_dir)
        attest_live_topology(args.provenance_dir)
        result = run_one(
            plan=plan, slot=args.slot, evidence_root=args.evidence_root,
            source_sha=args.expected_source_sha, provenance_hashes=hashes,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError, OverflowError) as exc:
        print(json.dumps({"decision": "BLOCK", "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
