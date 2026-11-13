#!/usr/bin/env python3
import copy
import http.client
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from request_fault_adapter import (
    FaultPlanValidationError,
    PROVIDER_TRANSITION,
    STATUS_INQUIRY,
    RequestFaultPlanAdapter,
)

LISTEN_ADDRESS = os.environ.get("PROXY_LISTEN_ADDRESS", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PROXY_LISTEN_PORT", "8080"))
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "payment-sandbox")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "8080"))

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class FaultState:
    def __init__(self):
        self.lock = threading.Lock()
        self.request_fault_plan = RequestFaultPlanAdapter()
        self.reset()

    def reset(self):
        self.drop_rule = None
        self.response_rule = None
        self.forwarded_by_path = {}
        self.responded_by_path = {}
        self.dropped = 0
        self.responded = 0
        self.last_dropped_path = None
        self.last_responded_path = None
        self.pending_async = {}
        self.provider_overlays = {}
        self.status_visibility = {}
        self.transition_history = []
        self.request_fault_plan.reset()

    def arm(self, method, path_suffix):
        with self.lock:
            self.drop_rule = {
                "method": method.upper(),
                "path_suffix": path_suffix,
            }

    def arm_response(self, method, path_suffix, status, error_code, message):
        with self.lock:
            self.response_rule = {
                "method": method.upper(),
                "path_suffix": path_suffix,
                "status": status,
                "error_code": error_code,
                "message": message,
            }

    def record_forward(self, method, path):
        key = f"{method.upper()} {path}"
        with self.lock:
            self.forwarded_by_path[key] = self.forwarded_by_path.get(key, 0) + 1

    def consume_response(self, method, path):
        with self.lock:
            rule = self.response_rule
            if (
                rule is not None
                and method.upper() == rule["method"]
                and path.endswith(rule["path_suffix"])
            ):
                self.response_rule = None
                self.responded += 1
                key = f"{method.upper()} {path}"
                self.responded_by_path[key] = self.responded_by_path.get(key, 0) + 1
                self.last_responded_path = key
                return dict(rule)
            return None

    def consume_drop(self, method, path):
        with self.lock:
            rule = self.drop_rule
            if (
                rule is not None
                and method.upper() == rule["method"]
                and path.endswith(rule["path_suffix"])
            ):
                self.drop_rule = None
                self.dropped += 1
                self.last_dropped_path = f"{method.upper()} {path}"
                return True
            return False

    def load_plan(self, payload):
        snapshot = self.request_fault_plan.load(payload)
        with self.lock:
            self.pending_async = {}
            self.provider_overlays = {}
            self.status_visibility = {}
            self.transition_history = []
        return snapshot

    def advance_plan(self, offset_ms):
        return self.request_fault_plan.advance(offset_ms)

    def consume_plan_before(self, method, path):
        return self.request_fault_plan.consume_before(method, path)

    def consume_plan_after_commit(self, method, path):
        return self.request_fault_plan.consume_after_commit(method, path)

    def register_pending_async(self, operation_id, request_record):
        with self.lock:
            if operation_id in self.pending_async:
                raise FaultPlanValidationError(
                    f"async operation already pending: {operation_id}"
                )
            self.pending_async[operation_id] = copy.deepcopy(request_record)

    def pending_async_operation(self, operation_id):
        with self.lock:
            operation = self.pending_async.get(operation_id)
            return copy.deepcopy(operation) if operation is not None else None

    def complete_async_success(self, operation_id, status_path, provider_view, upstream_status):
        with self.lock:
            self.pending_async.pop(operation_id, None)
            self.provider_overlays.pop(status_path, None)
            self.transition_history.append(
                {
                    "operation_id": operation_id,
                    "outcome": "SUCCEEDED",
                    "status_path": status_path,
                    "provider_side_effect": True,
                    "upstream_status": upstream_status,
                    "provider_view": copy.deepcopy(provider_view),
                }
            )

    def complete_async_failure(self, operation_id, status_path, provider_view):
        with self.lock:
            self.pending_async.pop(operation_id, None)
            self.provider_overlays[status_path] = copy.deepcopy(provider_view)
            self.transition_history.append(
                {
                    "operation_id": operation_id,
                    "outcome": "FAILED",
                    "status_path": status_path,
                    "provider_side_effect": False,
                    "upstream_status": None,
                    "provider_view": copy.deepcopy(provider_view),
                }
            )

    def provider_overlay(self, path):
        with self.lock:
            view = self.provider_overlays.get(path)
            return copy.deepcopy(view) if view is not None else None

    def apply_status_visibility(self, step):
        request_match = step["request_match"]
        key = (request_match["method"], request_match["path_suffix"])
        visibility = step["action"]["visibility"]
        with self.lock:
            if visibility == "IMMEDIATE":
                self.status_visibility.pop(key, None)
            else:
                previous = self.status_visibility.get(key)
                snapshots = (
                    copy.deepcopy(previous.get("snapshots", {}))
                    if previous is not None and previous.get("visibility") == "STALE"
                    else {}
                )
                self.status_visibility[key] = {
                    "step_id": step["step_id"],
                    "sequence": step["sequence"],
                    "visibility": visibility,
                    "request_match": copy.deepcopy(request_match),
                    "snapshots": snapshots if visibility == "STALE" else {},
                }

    def visibility_for(self, method, path):
        method = method.upper()
        with self.lock:
            matches = [
                rule
                for rule in self.status_visibility.values()
                if method == rule["request_match"]["method"]
                and path.endswith(rule["request_match"]["path_suffix"])
            ]
            if not matches:
                return None
            return copy.deepcopy(max(matches, key=lambda rule: rule["sequence"]))

    def stale_snapshot(self, method, path):
        method = method.upper()
        with self.lock:
            matches = [
                rule
                for rule in self.status_visibility.values()
                if rule["visibility"] == "STALE"
                and method == rule["request_match"]["method"]
                and path.endswith(rule["request_match"]["path_suffix"])
            ]
            if not matches:
                return None
            rule = max(matches, key=lambda item: item["sequence"])
            snapshot = rule["snapshots"].get(path)
            return copy.deepcopy(snapshot) if snapshot is not None else None

    def remember_stale_snapshot(self, method, path, provider_view):
        method = method.upper()
        with self.lock:
            matches = [
                rule
                for rule in self.status_visibility.values()
                if rule["visibility"] == "STALE"
                and method == rule["request_match"]["method"]
                and path.endswith(rule["request_match"]["path_suffix"])
            ]
            if not matches:
                return
            rule = max(matches, key=lambda item: item["sequence"])
            rule["snapshots"].setdefault(path, copy.deepcopy(provider_view))

    def capture_stale_before_transition(self, status_path, provider_view):
        self.remember_stale_snapshot("GET", status_path, provider_view)

    def snapshot(self):
        with self.lock:
            legacy = {
                "armed": self.drop_rule,
                "response_armed": self.response_rule,
                "forwarded_by_path": dict(sorted(self.forwarded_by_path.items())),
                "responded_by_path": dict(sorted(self.responded_by_path.items())),
                "dropped": self.dropped,
                "responded": self.responded,
                "last_dropped_path": self.last_dropped_path,
                "last_responded_path": self.last_responded_path,
                "pending_async": copy.deepcopy(self.pending_async),
                "provider_overlays": copy.deepcopy(self.provider_overlays),
                "status_visibility": copy.deepcopy(list(self.status_visibility.values())),
                "transition_history": copy.deepcopy(self.transition_history),
            }
        legacy["plan"] = self.request_fault_plan.snapshot()
        return legacy


STATE = FaultState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(
            json.dumps(
                {
                    "component": "payment-sandbox-fault-proxy",
                    "message": fmt % args,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def do_GET(self):
        if self.path == "/__fault/stats":
            self._json(200, STATE.snapshot())
            return
        if self.path == "/health/ready":
            self._proxy()
            return
        self._proxy()

    def do_POST(self):
        if self.path == "/__fault/reset":
            with STATE.lock:
                STATE.reset()
            self._json(200, STATE.snapshot())
            return
        if self.path == "/__fault/load-plan":
            body = self._read_body()
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                self._json(200, STATE.load_plan(payload))
            except (FaultPlanValidationError, TypeError, ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"error": f"invalid fault plan: {exc}"})
            return
        if self.path == "/__fault/advance":
            body = self._read_body()
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                result = STATE.advance_plan(payload["offset_ms"])
                executions = []
                for step in result["due_events"]:
                    executions.append(self._execute_timeline_step(step))
                response = STATE.snapshot()
                response["timeline_executions"] = executions
                self._json(200, response)
            except (KeyError, FaultPlanValidationError, TypeError, ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"error": f"invalid logical-time advance: {exc}"})
            except (OSError, http.client.HTTPException, RuntimeError) as exc:
                self._json(502, {"error": f"fault-plan transition execution failed: {exc}"})
            return
        if self.path == "/__fault/drop-next":
            body = self._read_body()
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                method = str(payload.get("method", "POST")).upper()
                path_suffix = str(payload["path_suffix"])
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"error": f"invalid fault rule: {exc}"})
                return
            if not path_suffix.startswith("/"):
                self._json(400, {"error": "path_suffix must begin with /"})
                return
            STATE.arm(method, path_suffix)
            self._json(200, STATE.snapshot())
            return
        if self.path == "/__fault/respond-next":
            body = self._read_body()
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                method = str(payload.get("method", "POST")).upper()
                path_suffix = str(payload["path_suffix"])
                status = int(payload["status"])
                error_code = str(payload.get("error_code", f"http_{status}"))
                message = str(payload.get("message", f"synthetic HTTP {status}"))
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"error": f"invalid response rule: {exc}"})
                return
            if not path_suffix.startswith("/"):
                self._json(400, {"error": "path_suffix must begin with /"})
                return
            if status < 400 or status > 599:
                self._json(400, {"error": "status must be between 400 and 599"})
                return
            STATE.arm_response(method, path_suffix, status, error_code, message)
            self._json(200, STATE.snapshot())
            return
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def _read_body(self):
        raw = self.headers.get("Content-Length")
        length = int(raw) if raw else 0
        return self.rfile.read(length) if length else b""

    def _proxy(self):
        body = self._read_body()
        try:
            self._proxy_with_body(body)
        except (OSError, http.client.HTTPException, RuntimeError, FaultPlanValidationError) as exc:
            self._json(502, {"error": f"upstream proxy failure: {exc}"})

    def _proxy_with_body(self, body):
        if self.command == "GET":
            visibility = STATE.visibility_for(self.command, self.path)
            if visibility is not None:
                if visibility["visibility"] == "NOT_FOUND":
                    self._json(
                        404,
                        {
                            "error": {
                                "code": "payment_not_found",
                                "message": "synthetic provider visibility gap",
                            }
                        },
                    )
                    return
                if visibility["visibility"] == "STALE":
                    snapshot = STATE.stale_snapshot(self.command, self.path)
                    if snapshot is None:
                        snapshot = self._read_effective_provider_view(self.path)
                        STATE.remember_stale_snapshot(self.command, self.path, snapshot)
                    self._json(200, snapshot)
                    return

            overlay = STATE.provider_overlay(self.path)
            if overlay is not None:
                self._json(200, overlay)
                return

        plan_before = STATE.consume_plan_before(self.command, self.path)
        if plan_before is not None:
            effect = plan_before["effect"]
            if effect == "DROP_BEFORE_UPSTREAM":
                self._drop_downstream()
                return
            if effect == "RESPOND_BEFORE_UPSTREAM":
                self._json(
                    plan_before["status"],
                    {
                        "error": {
                            "code": plan_before["error_code"],
                            "message": plan_before["message"],
                        }
                    },
                )
                return
            if effect == "DEFER_ASYNC":
                self._defer_async_request(plan_before, body)
                return
            raise RuntimeError(f"unsupported request fault effect: {effect}")

        response_rule = STATE.consume_response(self.command, self.path)
        if response_rule is not None:
            self._json(
                response_rule["status"],
                {
                    "error": {
                        "code": response_rule["error_code"],
                        "message": response_rule["message"],
                    }
                },
            )
            return

        status, response_headers, payload = self._send_upstream(
            self.command,
            self.path,
            body,
            self._forward_headers(body),
        )

        plan_after = STATE.consume_plan_after_commit(self.command, self.path)
        if plan_after is not None:
            self._drop_downstream()
            return

        if STATE.consume_drop(self.command, self.path):
            self._drop_downstream()
            return

        self._write_upstream_response(status, response_headers, payload)

    def _defer_async_request(self, plan_before, body):
        operation_id = plan_before["operation_id"]
        status_path = self._status_path_for_mutation(self.path)
        pending_view = self._read_effective_provider_view(status_path)
        STATE.register_pending_async(
            operation_id,
            {
                "method": self.command,
                "path": self.path,
                "status_path": status_path,
                "body": body.decode("utf-8"),
                "headers": self._forward_headers(body),
                "pending_view": pending_view,
            },
        )
        self._json(200, pending_view)

    def _execute_timeline_step(self, step):
        if step["point"] == STATUS_INQUIRY:
            STATE.apply_status_visibility(step)
            return {
                "step_id": step["step_id"],
                "point": step["point"],
                "visibility": step["action"]["visibility"],
            }

        if step["point"] != PROVIDER_TRANSITION:
            raise RuntimeError(f"unsupported timeline step in this checkpoint: {step['point']}")

        action = step["action"]
        operation_id = action["operation_id"]
        operation = STATE.pending_async_operation(operation_id)
        if operation is None:
            raise RuntimeError(
                f"provider transition has no pending runtime operation: {operation_id}"
            )

        status_path = operation["status_path"]
        pending_view = operation["pending_view"]
        STATE.capture_stale_before_transition(status_path, pending_view)

        if action["outcome"] == "FAILED":
            failed_view = copy.deepcopy(pending_view)
            failed_view["status"] = "failed"
            version = failed_view.get("version")
            if isinstance(version, int) and not isinstance(version, bool):
                failed_view["version"] = version + 1
            STATE.complete_async_failure(operation_id, status_path, failed_view)
            return {
                "step_id": step["step_id"],
                "point": step["point"],
                "operation_id": operation_id,
                "outcome": "FAILED",
                "provider_side_effect": False,
                "status_path": status_path,
            }

        if action["outcome"] != "SUCCEEDED":
            raise RuntimeError(f"unsupported provider transition outcome: {action['outcome']}")

        status, _, payload = self._send_upstream(
            operation["method"],
            operation["path"],
            operation["body"].encode("utf-8"),
            operation["headers"],
        )
        if status < 200 or status >= 300:
            raise RuntimeError(
                f"deferred provider mutation returned HTTP {status} for {operation_id}"
            )
        try:
            provider_view = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"deferred provider mutation returned invalid JSON for {operation_id}: {exc}"
            ) from exc
        if not isinstance(provider_view, dict):
            raise RuntimeError(
                f"deferred provider mutation returned non-object JSON for {operation_id}"
            )

        STATE.complete_async_success(operation_id, status_path, provider_view, status)
        return {
            "step_id": step["step_id"],
            "point": step["point"],
            "operation_id": operation_id,
            "outcome": "SUCCEEDED",
            "provider_side_effect": True,
            "status_path": status_path,
            "upstream_status": status,
        }

    def _read_effective_provider_view(self, path):
        overlay = STATE.provider_overlay(path)
        if overlay is not None:
            return overlay

        status, _, payload = self._send_upstream("GET", path, b"", {})
        if status < 200 or status >= 300:
            raise RuntimeError(f"provider status lookup returned HTTP {status}: {path}")
        try:
            provider_view = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"provider status lookup returned invalid JSON: {path}") from exc
        if not isinstance(provider_view, dict):
            raise RuntimeError(f"provider status lookup returned non-object JSON: {path}")
        return provider_view

    def _send_upstream(self, method, path, body, headers):
        connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=10)
        try:
            connection.request(method, path, body=body or None, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            response_headers = response.getheaders()
            STATE.record_forward(method, path)
            return response.status, response_headers, payload
        finally:
            connection.close()

    def _forward_headers(self, body):
        headers = {}
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP or lower in {"host", "content-length"}:
                continue
            headers[name] = value
        if body:
            headers["Content-Length"] = str(len(body))
        return headers

    @staticmethod
    def _status_path_for_mutation(path):
        parts = path.split("?")[0].rstrip("/").split("/")
        if len(parts) < 4 or parts[1] != "payments":
            raise FaultPlanValidationError(
                f"DEFER_ASYNC requires /payments/<id>/<operation> path: {path}"
            )
        return "/".join(parts[:-1]) or "/"

    def _write_upstream_response(self, status, response_headers, payload):
        self.send_response(status)
        for name, value in response_headers:
            lower = name.lower()
            if lower in HOP_BY_HOP or lower == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _drop_downstream(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()
        self.close_connection = True

    def _json(self, status, payload):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_ADDRESS, LISTEN_PORT), Handler)
    print(
        json.dumps(
            {
                "component": "payment-sandbox-fault-proxy",
                "listen": f"{LISTEN_ADDRESS}:{LISTEN_PORT}",
                "upstream": f"{UPSTREAM_HOST}:{UPSTREAM_PORT}",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    server.serve_forever()
