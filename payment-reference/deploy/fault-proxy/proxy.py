#!/usr/bin/env python3
import http.client
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from request_fault_adapter import FaultPlanValidationError, RequestFaultPlanAdapter

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
        return self.request_fault_plan.load(payload)

    def advance_plan(self, offset_ms):
        return self.request_fault_plan.advance(offset_ms)

    def consume_plan_before(self, method, path):
        return self.request_fault_plan.consume_before(method, path)

    def consume_plan_after_commit(self, method, path):
        return self.request_fault_plan.consume_after_commit(method, path)

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
                self._json(200, STATE.advance_plan(payload["offset_ms"]))
            except (KeyError, FaultPlanValidationError, TypeError, ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"error": f"invalid logical-time advance: {exc}"})
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

        plan_before = STATE.consume_plan_before(self.command, self.path)
        if plan_before is not None:
            if plan_before["effect"] == "DROP_BEFORE_UPSTREAM":
                self._drop_downstream()
                return
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

        headers = {}
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP or lower in {"host", "content-length"}:
                continue
            headers[name] = value
        if body:
            headers["Content-Length"] = str(len(body))

        connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=10)
        try:
            connection.request(self.command, self.path, body=body or None, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            STATE.record_forward(self.command, self.path)

            # The upstream response is fully consumed before the downstream
            # connection is dropped. This deterministically models
            # commit-success / response-loss ambiguity.
            plan_after = STATE.consume_plan_after_commit(self.command, self.path)
            if plan_after is not None:
                self._drop_downstream()
                return

            if STATE.consume_drop(self.command, self.path):
                self._drop_downstream()
                return

            self.send_response(response.status)
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in HOP_BY_HOP or lower == "content-length":
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except (OSError, http.client.HTTPException) as exc:
            self._json(502, {"error": f"upstream proxy failure: {exc}"})
        finally:
            connection.close()

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
