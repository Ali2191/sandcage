"""The HTTP server: the standard library's threading server in front of :class:`App`.

- **Bodies** are capped at :data:`BODY_LIMIT` bytes. An over-limit ``Content-Length`` is
  refused with 413 *before a byte of the body is read*; a chunked body is refused (411),
  and the server never reads past the declared length, so a missing or false length
  cannot get around the cap. Every response closes the connection, so an unread body is
  never parsed as a next request; the close lingers briefly (bounded) so the client
  still reads the refusal instead of a reset.
- **CORS** only for the one configured panel origin: GET and POST, the headers
  ``Content-Type``, ``Last-Event-ID`` and ``X-Access-Code``, ``Retry-After`` exposed.
- **Logs** carry the method, the path (no query) and the status. Never a header, so never
  the access code.
- **Event streams** are written event by event and flushed; a client that leaves ends the
  stream on the next write.

Production sits behind Caddy (TLS, the same body cap, slow clients); each connection has
a read timeout so an idle socket cannot hold a thread for ever.
"""

from __future__ import annotations

import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, Final
from urllib.parse import parse_qs, urlsplit

from sandcage.api.app import App, EventStream, Request, Response
from sandcage.api.settings import BODY_LIMIT

log = logging.getLogger("sandcage.api")

READ_TIMEOUT: Final = 15.0
LINGER_SECONDS: Final = 1.0
LINGER_BYTES: Final = 2 * BODY_LIMIT
ALLOWED_HEADERS: Final = "Content-Type, Last-Event-ID, X-Access-Code"
EXPOSED_HEADERS: Final = "Retry-After"
_SECURITY_HEADERS: Final = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class Handler(BaseHTTPRequestHandler):
    app: ClassVar[App]
    server_version = "sandcage"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = READ_TIMEOUT

    # --- methods -------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_OPTIONS(self) -> None:
        headers = self._cors(preflight=True)
        self._send(Response(204, headers=headers))

    # --- one request ---------------------------------------------------------------------

    def _serve(self) -> None:
        self.close_connection = True
        parts = urlsplit(self.path)
        if self.headers.get("Transfer-Encoding"):
            self._send(Response.error(411, "Send the body with a Content-Length."))
            return
        length_text = self.headers.get("Content-Length", "0").strip() or "0"
        if not length_text.isdigit() or len(length_text) > 12:
            self._send(Response.error(400, "The Content-Length is not a number."))
            return
        length = int(length_text)
        if length > BODY_LIMIT:
            self._send(Response.error(413, f"The request body is over {BODY_LIMIT} bytes."))
            return
        try:
            body = self.rfile.read(length) if length else b""
        except (TimeoutError, OSError):
            return  # the client went quiet or away mid-body: nothing to answer
        if len(body) != length:
            self._send(Response.error(400, "The request body is shorter than its length."))
            return
        query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
        request = Request(
            method=self.command,
            path=parts.path,
            query=query,
            headers={name.lower(): value for name, value in self.headers.items()},
            body=body,
            peer=self.client_address[0] if self.client_address else "",
        )
        try:
            result = self.app.handle(request)
        except Exception as problem:  # noqa: BLE001 - any bug answers 500, never a trace
            log.error("%s %s failed: %s", self.command, parts.path, type(problem).__name__)
            result = Response.error(500, "Something went wrong on the server.")
        if isinstance(result, EventStream):
            self._stream(result)
        else:
            self._send(result)

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        headers = {**_SECURITY_HEADERS, **self._cors(preflight=False), **response.headers}
        if response.status != 204:
            headers["Content-Type"] = response.content_type
            headers["Content-Length"] = str(len(response.body))
        headers["Connection"] = "close"
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if response.status != 204 and self.command != "HEAD":
            self.wfile.write(response.body)

    def _stream(self, stream: EventStream) -> None:
        self.send_response(200)
        headers = {
            **_SECURITY_HEADERS,
            **self._cors(preflight=False),
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "close",
        }
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

        def write(chunk: bytes) -> None:
            self.wfile.write(chunk)
            self.wfile.flush()

        try:
            self.app.stream(stream, write)
        except OSError:
            return  # the client left (broken pipe, reset or timeout)

    def _cors(self, *, preflight: bool) -> dict[str, str]:
        allowed = self.app.settings.panel_origin
        origin = self.headers.get("Origin")
        if allowed is None:
            return {}
        if origin != allowed:
            return {"Vary": "Origin"}
        headers = {
            "Access-Control-Allow-Origin": allowed,
            "Vary": "Origin",
            "Access-Control-Expose-Headers": EXPOSED_HEADERS,
        }
        if preflight:
            headers["Access-Control-Allow-Methods"] = "GET, POST"
            headers["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
            headers["Access-Control-Max-Age"] = "600"
        return headers

    # --- logging: method, path and status only ------------------------------------------

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        path = urlsplit(getattr(self, "path", "")).path
        log.info("%s %s %s", getattr(self, "command", "-"), path, code)

    def log_error(self, format: str, *args: object) -> None:
        log.warning("bad request from a client (%s)", type(self).__name__)

    def log_message(self, format: str, *args: object) -> None:
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def shutdown_request(self, request: socket.socket) -> None:  # type: ignore[override]
        """Lingering close: stop sending, then discard (never parse) a bounded amount of
        whatever the client is still sending, so closing with unread bytes does not reset
        the connection before the client has read our answer (a 413, say)."""
        try:
            request.shutdown(socket.SHUT_WR)
            request.settimeout(LINGER_SECONDS)
            left = LINGER_BYTES
            while left > 0 and (chunk := request.recv(min(left, 65536))):
                left -= len(chunk)
        except OSError:
            pass  # the client is gone already: nothing to protect
        self.close_request(request)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def make_server(app: App, host: str, port: int) -> Server:
    """A server for ``app`` (port 0 picks a free port; read it from ``server_address``)."""
    handler = type("SandcageHandler", (Handler,), {"app": app})
    return Server((host, port), handler)
