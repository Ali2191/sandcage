"""A stand-in for the deployed edge (Caddy + API) for testing scripts/verify_deploy.py: a stdlib
HTTP server that behaves like the real thing in the way the verifier checks, and that can be
switched into one specific bad behaviour per instance. Plain HTTP on loopback."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

GOOD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; frame-ancestors 'none'; script-src 'self'"
    ),
}
RUN_ID = "0123456789ab"


class EdgeState:
    def __init__(self, code: str, bad: str | None) -> None:
        self.code = code
        self.bad = bad
        self.stopped = threading.Event()
        self.started = 0


class Handler(BaseHTTPRequestHandler):
    state: ClassVar[EdgeState]
    protocol_version = "HTTP/1.1"
    server_version = "fake"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_response(self, code: int, message: str | None = None) -> None:
        # The base class adds a Server header; the real edge removes it, so this one sends none.
        self.send_response_only(code, message)

    # --- helpers -------------------------------------------------------------------------

    def _send(self, status: int, body: bytes = b"", extra: dict[str, str] | None = None) -> None:
        bad = self.state.bad
        self.send_response(status)
        headers = dict(GOOD_HEADERS)
        if bad == "server_header":
            headers["Server"] = "Caddy"
        if bad == "cors_star":
            headers["Access-Control-Allow-Origin"] = "*"
        if bad == "compressed_api" and self.path.startswith("/api/"):
            headers["Content-Encoding"] = "gzip"
        if bad == "no_csp":
            del headers["Content-Security-Policy"]
        headers.update(extra or {})
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value).encode(), {"Content-Type": "application/json"})

    def _code_ok(self) -> bool:
        return self.headers.get("X-Access-Code") == self.state.code

    # --- routes --------------------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, b"<html>panel</html>", {"Content-Type": "text/html"})
        elif path == "/api/healthz":
            self._json(200, {"ok": True})
        elif path == "/api/readyz":
            self._readyz()
        elif path == "/api/demo/status":
            self._json(200, {"paused": self.state.bad == "paused"})
        elif path == f"/api/runs/{RUN_ID}/events":
            self._events()
        else:
            self._json(404, {"detail": "Not found."})

    def _readyz(self) -> None:
        runtime = {"ok": True, "label": "gVisor (runsc)", "gvisor": True}
        if self.state.bad == "runc":
            runtime = {"ok": True, "label": "runc (plain Docker)", "gvisor": False}
        if self.state.bad == "no_runtime":
            runtime = {"ok": True}
        checks: dict[str, object] = {"store": {"ok": True}, "runtime": runtime}
        if self.state.bad == "not_ready":
            checks["image"] = {"ok": False, "detail": "missing"}
        body: dict[str, object] = {"ready": self.state.bad != "not_ready", "checks": checks}
        if self.state.bad == "leak_code":
            body["debug"] = {"access_code": self.state.code}
        if self.state.bad == "leak_key":
            body["debug"] = {"api_key": "sk-" + "A1b2C3d4" * 4}
        self._json(200 if self.state.bad != "not_ready" else 503, body)

    def do_OPTIONS(self) -> None:
        extra = {}
        if self.state.bad == "cors_preflight_open":
            extra = {"Access-Control-Allow-Origin": self.headers.get("Origin", "")}
        self._send(204, b"", extra)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024 and self.state.bad != "no_413":
            self._json(413, {"detail": "The request body is over 65536 bytes."})
            return
        body = self.rfile.read(min(length, 70 * 1024)) if length else b""
        del body
        if path == "/api/demo/unlock":
            self._unlock()
        elif path == "/api/runs":
            self._start()
        elif path == f"/api/runs/{RUN_ID}/stop":
            self.state.stopped.set()
            self._json(200, {"status": "stopped"})
        else:
            self._json(404, {"detail": "Not found."})

    def _unlock(self) -> None:
        if self.state.bad == "accepts_no_code":
            self._json(200, {"ok": True})
        elif self.headers.get("X-Access-Code") is None:
            self._json(401, {"detail": "An access code is needed."})
        elif not self._code_ok():
            self._json(403, {"detail": "That code is not right."})
        else:
            self._json(200, {"ok": True})

    def _start(self) -> None:
        if self.state.bad == "paused":
            self._json(503, {"detail": "paused"})
        elif not self._code_ok():
            self._json(403, {"detail": "no"})
        else:
            self.state.started += 1
            self._json(202, {"id": RUN_ID, "run_id": RUN_ID})

    # --- the event stream ----------------------------------------------------------------

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode())
        self.wfile.flush()

    def _events(self) -> None:
        bad = self.state.bad
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = int(self.headers.get("Last-Event-ID") or 0)
        if bad == "replay_on_resume":
            last = 0
        if bad == "buffered_sse":
            # What a buffering proxy does: nothing reaches the client until the run is over.
            self.state.stopped.wait(30)
            self._write("id: 1\nevent: step\ndata: {}\n\n")
            return
        events = [(1, "started"), (2, "step"), (3, "step"), (4, "approval_needed")]
        for number, kind in events:
            if number > last:
                self._write(f"id: {number}\nevent: {kind}\ndata: {{}}\n\n")
        while not self.state.stopped.is_set():
            if bad != "no_heartbeat":
                self._write(": ping\n\n")
            if self.state.stopped.wait(0.4):
                break
        for number, kind in [(5, "stopped"), (6, "done")]:
            if number > last:
                self._write(f"id: {number}\nevent: {kind}\ndata: {{}}\n\n")


@contextmanager
def edge(code: str, bad: str | None = None) -> Iterator[tuple[str, EdgeState]]:
    state = EdgeState(code, bad)
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        state.stopped.set()
        server.shutdown()
        server.server_close()
        time.sleep(0.01)
