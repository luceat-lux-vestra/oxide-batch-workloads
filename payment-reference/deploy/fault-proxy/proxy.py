#!/usr/bin/env python3
import http.client
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        self.reset()

    def reset(self):
        self.drop_rule = None
        self.forwarded_by_path = {}
        self.dropped = 0
        self.last_dropped_path = None

    def arm(self, method, path_suffix):
        with self.lock:
            self.drop_rule = {
                "method": method.upper(),
                "path_suffix": path_suffix,
            }

    def record_forward(self, method, path):
        key = f"{method.upper()} {path}"
        with self.lock:
            self.forwarded_by_path[key] = self.forwarded_by_path.get(key, 0) + 1

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

    def snapshot(self):
        with self.lock:
            return {
                "armed": self.drop_rule,
                "forwarded_by_path": dict(sorted(self.forwarded_by_path.items())),
                "dropped": self.dropped,
                "last_dropped_path": self.last_dropped_path,
            }


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
            if STATE.consume_drop(self.command, self.path):
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                self.close_connection = True
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
