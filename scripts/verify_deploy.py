"""Check a deployed Sandcage from the outside and print a pass/fail checklist.

    python3 scripts/verify_deploy.py https://203-0-113-7.sslip.io --code-file ~/code.txt
    python3 scripts/verify_deploy.py https://localhost --rehearsal --code-file ~/code.txt

Exit status 0 when nothing failed, 1 when anything did. Standard library only.

What it checks (each line is PASS, FAIL, WARN or UNTESTED, with the reason):
  * HTTPS with a valid certificate; plain HTTP redirects to HTTPS
  * /healthz and /readyz; readyz names the sandbox runtime, and warns loudly about plain Docker
  * a changing call without the access code, or with a wrong one, is refused; the right one works
  * the kill switch (only when it is on: it cannot be toggled from the outside)
  * a live event stream really streams through Caddy: the first event arrives while the run is
    still waiting for you, a heartbeat shows, and Last-Event-ID resume works
  * an oversized body is refused without being read
  * CORS never allows another origin; security headers are present; no Server header;
    the API is not compressed
  * Docker's ports, the API port and the panel port are not reachable from here
  * the access code and key shapes never appear in anything the server sent

The access code is read from --code-file (mode 600) and is never printed. The stream checks start
one scripted Bench run (no model call, no tokens) and stop it again; that run counts against the
per-client hourly limit.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import socket
import ssl
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

PASS: Final = "PASS"  # noqa: S105 - a status word, not a password
FAIL: Final = "FAIL"
WARN: Final = "WARN"
UNTESTED: Final = "UNTESTED"

TIMEOUT: Final = 15.0
FIRST_EVENT_SECONDS: Final = 5.0
HEARTBEAT_WAIT_SECONDS: Final = 30.0
RESUME_WAIT_SECONDS: Final = 25.0
OVERSIZE_BYTES: Final = 70 * 1024
EVIL_ORIGIN: Final = "https://evil.example"
#: Ports that must not answer from outside: Docker (plain and TLS), the API, the panel, Caddy's
#: admin, and the alternates a misconfigured compose file might publish. 80 and 443 are the
#: only public ones; 22 is reported.
CLOSED_PORTS: Final = (2375, 2376, 8000, 8001, 3000, 2019, 8080, 8443)
SSH_PORT: Final = 22
REQUIRED_HEADERS: Final = (
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "content-security-policy",
    "permissions-policy",
)
LEAK_SHAPES: Final = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|llm_api_key|secret)\b\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_./+-]{12,}"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_.~+/=-]{16,}"),
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    seconds: float

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class Target:
    host: str
    port: int
    tls: bool
    rehearsal: bool

    @property
    def origin(self) -> str:
        scheme = "https" if self.tls else "http"
        default = 443 if self.tls else 80
        suffix = "" if self.port == default else f":{self.port}"
        return f"{scheme}://{self.host}{suffix}"


@dataclass
class Session:
    """One verification run: the target, the optional code, and everything the server sent."""

    target: Target
    code: str | None
    seen: list[str] = field(default_factory=list)

    def context(self) -> ssl.SSLContext | None:
        if not self.target.tls:
            return None
        context = ssl.create_default_context()
        if self.target.rehearsal:  # a local rehearsal uses Caddy's own certificate authority
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def connection(self, timeout: float = TIMEOUT) -> http.client.HTTPConnection:
        target = self.target
        if target.tls:
            return http.client.HTTPSConnection(
                target.host, target.port, timeout=timeout, context=self.context()
            )
        return http.client.HTTPConnection(target.host, target.port, timeout=timeout)

    def record(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        head = "\n".join(f"{key}: {value}" for key, value in headers.items())
        self.seen.append(f"{status}\n{head}\n{body.decode('utf-8', errors='replace')}")

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        with_code: bool = False,
    ) -> Response:
        sent = dict(headers or {})
        if with_code and self.code is not None:
            sent["X-Access-Code"] = self.code
        connection = self.connection()
        started = time.monotonic()
        try:
            connection.request(method, path, body=body, headers=sent)
            reply = connection.getresponse()
            data = reply.read()
            result = Response(
                reply.status,
                {key.lower(): value for key, value in reply.getheaders()},
                data,
                time.monotonic() - started,
            )
        finally:
            connection.close()
        self.record(result.status, result.headers, result.body)
        return result


# --- pure judgements (unit-tested on their own) ---------------------------------------------------


def judge_headers(headers: Mapping[str, str], *, over_tls: bool) -> list[str]:
    """What is wrong with the security headers of a panel response (empty list: nothing)."""
    lowered = {key.lower(): value for key, value in headers.items()}
    wrong: list[str] = []
    needed = (
        REQUIRED_HEADERS
        if over_tls
        else tuple(h for h in REQUIRED_HEADERS if h != REQUIRED_HEADERS[0])
    )
    wrong.extend(f"missing {name}" for name in needed if name not in lowered)
    if "server" in lowered:
        wrong.append(f"Server header present ({lowered['server']})")
    policy = lowered.get("content-security-policy", "")
    for directive in ("default-src 'self'", "object-src 'none'", "frame-ancestors 'none'"):
        if policy and directive not in policy:
            wrong.append(f"CSP lacks {directive}")
    if "unsafe-eval" in policy:
        wrong.append("CSP allows unsafe-eval")
    if lowered.get("x-content-type-options", "nosniff") != "nosniff":
        wrong.append("X-Content-Type-Options is not nosniff")
    return wrong


def judge_cors(headers: Mapping[str, str], origin: str) -> str | None:
    """None when the response does not let ``origin`` read it, else the problem."""
    lowered = {key.lower(): value for key, value in headers.items()}
    allowed = lowered.get("access-control-allow-origin")
    if allowed is None:
        return None
    if allowed in {"*", origin}:
        return f"Access-Control-Allow-Origin is {allowed!r} for {origin}"
    return None


def find_leaks(seen: Iterable[str], code: str | None) -> list[str]:
    """Descriptions (never the secret itself) of anything secret-shaped in what the server sent."""
    problems: list[str] = []
    for index, text in enumerate(seen):
        if code is not None and code in text:
            problems.append(f"response #{index + 1} contains the access code")
        for shape in LEAK_SHAPES:
            if shape.search(text):
                problems.append(f"response #{index + 1} contains something shaped like a key")
                break
    return problems


@dataclass
class StreamEvent:
    id: int | None
    kind: str
    data: str


def parse_sse_line(line: str, pending: dict[str, str]) -> StreamEvent | str | None:
    """Feed one line of an event stream. Returns an event when a blank line completes one, the
    text ``ping`` for a comment line, else None."""
    line = line.rstrip("\r\n")
    if line == "":
        if not pending:
            return None
        event = StreamEvent(
            int(pending["id"]) if pending.get("id", "").isdigit() else None,
            pending.get("event", "message"),
            pending.get("data", ""),
        )
        pending.clear()
        return event
    if line.startswith(":"):
        return "ping"
    name, _, value = line.partition(":")
    pending[name] = value.removeprefix(" ")
    return None


# --- the checks ----------------------------------------------------------------------------------


def check_tls(session: Session) -> Check:
    target = session.target
    if not target.tls:
        status = UNTESTED if target.rehearsal else FAIL
        return Check("https certificate", status, "the URL is not https")
    if target.rehearsal:
        return Check(
            "https certificate",
            UNTESTED,
            "rehearsal: a self-signed local certificate is accepted; real certificates need the VM",
        )
    try:
        context = ssl.create_default_context()
        with (
            socket.create_connection((target.host, target.port), timeout=TIMEOUT) as raw,
            context.wrap_socket(raw, server_hostname=target.host) as wrapped,
        ):
            certificate = wrapped.getpeercert()
    except (ssl.SSLError, OSError) as problem:
        return Check("https certificate", FAIL, f"{type(problem).__name__}: {problem}")
    expires = certificate.get("notAfter") if certificate else None
    if not isinstance(expires, str):
        return Check("https certificate", FAIL, "the server sent no certificate details")
    days = (ssl.cert_time_to_seconds(expires) - time.time()) / 86400
    if days < 7:
        return Check("https certificate", FAIL, f"valid, but expires in {days:.1f} days")
    return Check("https certificate", PASS, f"valid for {target.host}, {days:.0f} days left")


def check_http_redirect(session: Session) -> Check:
    target = session.target
    if not target.tls:
        return Check("http redirects to https", UNTESTED, "the URL is plain http")
    try:
        connection = http.client.HTTPConnection(target.host, 80, timeout=TIMEOUT)
        connection.request("GET", "/", headers={"Host": target.host})
        reply = connection.getresponse()
        reply.read()
        location = reply.getheader("Location") or ""
        connection.close()
    except OSError as problem:
        return Check("http redirects to https", FAIL, f"port 80 did not answer ({problem})")
    if reply.status in (301, 302, 307, 308) and location.startswith("https://"):
        return Check("http redirects to https", PASS, f"{reply.status} to https")
    return Check("http redirects to https", FAIL, f"status {reply.status}, Location {location!r}")


def check_health(session: Session) -> Check:
    try:
        reply = session.request("GET", "/api/healthz")
    except OSError as problem:
        return Check("healthz", FAIL, f"no answer ({problem})")
    if reply.status == 200 and json.loads(reply.text() or "{}").get("ok") is True:
        return Check("healthz", PASS, "the API process answers through the edge")
    return Check("healthz", FAIL, f"status {reply.status}")


def check_ready(session: Session) -> list[Check]:
    try:
        reply = session.request("GET", "/api/readyz")
        data = json.loads(reply.text())
    except (OSError, ValueError) as problem:
        return [Check("readyz", FAIL, f"no usable answer ({problem})")]
    checks = data.get("checks", {})
    failing = [name for name, found in checks.items() if not found.get("ok")]
    results: list[Check] = []
    if reply.status == 200 and data.get("ready") is True and not failing:
        results.append(Check("readyz", PASS, f"ready: {', '.join(sorted(checks))}"))
    else:
        results.append(Check("readyz", FAIL, f"not ready, failing: {', '.join(failing) or '?'}"))
    runtime = checks.get("runtime", {})
    label = runtime.get("label")
    if not label:
        results.append(Check("sandbox runtime stated", FAIL, "readyz does not say which runtime"))
    elif runtime.get("gvisor") is True:
        results.append(Check("sandbox runtime stated", PASS, f"{label}"))
    else:
        results.append(
            Check(
                "sandbox runtime stated",
                WARN,
                f"{label}: gVisor is NOT in use, sandboxes run in plain Docker "
                "(the panel says so too; fix it before claiming gVisor anywhere)",
            )
        )
    return results


def check_code_refusals(session: Session) -> list[Check]:
    results: list[Check] = []
    route = "/api/demo/unlock"
    try:
        none = session.request("POST", route, body=b"{}")
        wrong = session.request(
            "POST", route, body=b"{}", headers={"X-Access-Code": "wrong-" + uuid.uuid4().hex}
        )
    except OSError as problem:
        return [Check("changing call refused without the code", FAIL, f"no answer ({problem})")]
    refused = (401, 403)
    results.append(
        Check(
            "changing call refused without the code",
            PASS if none.status in refused else FAIL,
            f"status {none.status} (needs 401 or 403)",
        )
    )
    results.append(
        Check(
            "changing call refused with a wrong code",
            PASS if wrong.status in refused else FAIL,
            f"status {wrong.status} (needs 401 or 403)",
        )
    )
    if session.code is None:
        results.append(
            Check("right code accepted", UNTESTED, "give --code-file to test the right code")
        )
        return results
    right = session.request("POST", route, body=b"{}", with_code=True)
    results.append(
        Check(
            "right code accepted",
            PASS if right.status == 200 else FAIL,
            f"status {right.status}"
            + (" (rate limited: wait and retry)" if right.status == 429 else ""),
        )
    )
    return results


def check_kill_switch(session: Session) -> Check:
    name = "kill switch honoured"
    try:
        status = json.loads(session.request("GET", "/api/demo/status").text())
    except (OSError, ValueError) as problem:
        return Check(name, FAIL, f"could not read /demo/status ({problem})")
    if status.get("paused") is not True:
        return Check(
            name,
            UNTESTED,
            "the switch is off and cannot be toggled from outside; turn it on by hand "
            "(RUNBOOK) and run this again to test it",
        )
    if session.code is None:
        return Check(name, UNTESTED, "paused, but --code-file is needed to try starting a run")
    body = json.dumps({"scenario": "demo"}).encode()
    reply = session.request(
        "POST", "/api/runs", body=body, with_code=True, headers={"Content-Type": "application/json"}
    )
    ok = reply.status == 503
    return Check(name, PASS if ok else FAIL, f"paused: starting a run answered {reply.status}")


class StreamFailureError(Exception):
    """The event stream check cannot go on; the message says why."""


def open_stream(session: Session, run_id: str, last_id: int | None) -> http.client.HTTPResponse:
    headers = {"Accept": "text/event-stream", "X-Access-Code": session.code or ""}
    if last_id is not None:
        headers["Last-Event-ID"] = str(last_id)
    connection = session.connection(timeout=HEARTBEAT_WAIT_SECONDS + 10)
    connection.request("GET", f"/api/runs/{run_id}/events", headers=headers)
    reply = connection.getresponse()
    if reply.status != 200:
        connection.close()
        raise StreamFailureError(f"the event stream answered {reply.status}")
    return reply


def read_stream(
    reply: http.client.HTTPResponse, *, stop: Callable[[StreamEvent | str], bool], seconds: float
) -> list[StreamEvent | str]:
    """Read lines until ``stop`` says so, the stream ends, or ``seconds`` pass."""
    found: list[StreamEvent | str] = []
    pending: dict[str, str] = {}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            line = reply.fp.readline().decode("utf-8", errors="replace") if reply.fp else ""
        except (TimeoutError, OSError):
            break
        if line == "":
            break
        item = parse_sse_line(line, pending)
        if item is None:
            continue
        found.append(item)
        if stop(item):
            break
    return found


def start_scripted_run(session: Session) -> str:
    body = json.dumps({"scenario": "demo"}).encode()
    reply = session.request(
        "POST", "/api/runs", body=body, with_code=True, headers={"Content-Type": "application/json"}
    )
    if reply.status == 429:
        raise StreamFailureError("rate limited (the per-client hourly limit): wait and run again")
    if reply.status == 503:
        raise StreamFailureError("the demo is paused (kill switch on): no run can be started")
    if reply.status != 202:
        raise StreamFailureError(f"starting the scripted run answered {reply.status}")
    return str(json.loads(reply.text())["id"])


def check_streaming(session: Session) -> list[Check]:
    names = ("event stream streams", "stream heartbeat", "Last-Event-ID resume")
    if session.code is None:
        return [Check(name, UNTESTED, "give --code-file: a stream needs a run") for name in names]
    run_id = ""
    try:
        run_id = start_scripted_run(session)
        return stream_checks(session, run_id)
    except StreamFailureError as problem:
        return [Check(name, FAIL, str(problem)) for name in names]
    except OSError as problem:
        return [Check(name, FAIL, f"{type(problem).__name__}: {problem}") for name in names]
    finally:
        if run_id:
            # If this fails the run ends by itself at the 120 s approval timeout.
            with contextlib.suppress(OSError):
                session.request("POST", f"/api/runs/{run_id}/stop", with_code=True, body=b"{}")


def stream_checks(session: Session, run_id: str) -> list[Check]:
    results: list[Check] = []
    started = time.monotonic()
    first = open_stream(session, run_id, None)
    try:
        events = read_stream(
            first,
            stop=lambda item: isinstance(item, StreamEvent) and item.kind == "approval_needed",
            seconds=FIRST_EVENT_SECONDS * 4,
        )
        first_seen = [e for e in events if isinstance(e, StreamEvent)]
        if not first_seen:
            return [
                Check("event stream streams", FAIL, "no event arrived: the stream may be buffered"),
                Check("stream heartbeat", UNTESTED, "no stream"),
                Check("Last-Event-ID resume", UNTESTED, "no stream"),
            ]
        waiting = any(e.kind == "approval_needed" for e in first_seen)
        arrived = time.monotonic() - started
        detail = (
            f"{len(first_seen)} events in {arrived:.1f}s, and the run was still waiting for you"
        )
        streams = waiting and arrived < FIRST_EVENT_SECONDS * 4
        results.append(
            Check(
                "event stream streams",
                PASS if streams else FAIL,
                detail
                if streams
                else "events arrived only in a burst, or the run had already ended",
            )
        )
        last_id = max((e.id for e in first_seen if e.id is not None), default=0)
        beat = read_stream(first, stop=lambda item: item == "ping", seconds=HEARTBEAT_WAIT_SECONDS)
        results.append(
            Check(
                "stream heartbeat",
                PASS if "ping" in beat else FAIL,
                "a keep-alive comment arrived while the run was idle"
                if "ping" in beat
                else f"no heartbeat in {HEARTBEAT_WAIT_SECONDS:.0f}s of silence",
            )
        )
    finally:
        first.close()
    results.append(resume_check(session, run_id, last_id))
    return results


def resume_check(session: Session, run_id: str, last_id: int) -> Check:
    name = "Last-Event-ID resume"
    second = open_stream(session, run_id, last_id)
    try:
        session.request("POST", f"/api/runs/{run_id}/stop", with_code=True, body=b"{}")
        events = read_stream(
            second,
            stop=lambda item: isinstance(item, StreamEvent) and item.kind == "done",
            seconds=RESUME_WAIT_SECONDS,
        )
    finally:
        second.close()
    seen = [e for e in events if isinstance(e, StreamEvent)]
    replayed = [e.id for e in seen if e.id is not None and e.id <= last_id]
    if replayed:
        return Check(name, FAIL, f"events already seen were sent again: ids {replayed[:5]}")
    if not any(e.kind == "done" for e in seen):
        return Check(
            name, FAIL, "after reconnecting with Last-Event-ID the stream never delivered the end"
        )
    return Check(name, PASS, f"resumed after id {last_id}: only new events, then the end")


def check_oversize(session: Session) -> Check:
    name = "oversized body refused unread"
    target = session.target
    try:
        raw = socket.create_connection((target.host, target.port), timeout=TIMEOUT)
        stream: socket.socket | ssl.SSLSocket = raw
        context = session.context()
        if context is not None:
            stream = context.wrap_socket(raw, server_hostname=target.host)
        stream.settimeout(TIMEOUT)
        head = (
            f"POST /api/runs HTTP/1.1\r\nHost: {target.host}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {10 * 1024 * 1024}\r\nConnection: close\r\n\r\n"
        )
        started = time.monotonic()
        stream.sendall(head.encode() + b'{"repo": "x"')  # the other ten megabytes never come
        answer = stream.recv(4096).decode("latin-1", errors="replace")
        seconds = time.monotonic() - started
        stream.close()
    except (OSError, ssl.SSLError) as problem:
        return Check(name, FAIL, f"{type(problem).__name__}: {problem}")
    session.seen.append(answer)
    status = answer.split(" ", 2)[1] if answer.startswith("HTTP/") else "?"
    if status == "413" and seconds < FIRST_EVENT_SECONDS:
        return Check(name, PASS, f"413 in {seconds:.1f}s without sending the body")
    return Check(name, FAIL, f"expected 413 straight away, got {status} after {seconds:.1f}s")


def check_oversize_sent(session: Session) -> Check:
    name = "body over 64 KB refused"
    try:
        reply = session.request(
            "POST",
            "/api/runs",
            body=b"x" * OVERSIZE_BYTES,
            headers={"Content-Type": "application/json"},
            with_code=True,
        )
    except OSError as problem:
        # A server that closes while we are still sending has refused, too; say how.
        return Check(
            name, PASS, f"the connection was closed while sending ({type(problem).__name__})"
        )
    return Check(name, PASS if reply.status == 413 else FAIL, f"status {reply.status} (needs 413)")


def check_cors(session: Session) -> Check:
    name = "CORS allows no other origin"
    problems: list[str] = []
    try:
        plain = session.request("GET", "/api/healthz", headers={"Origin": EVIL_ORIGIN})
        preflight = session.request(
            "OPTIONS",
            "/api/runs",
            headers={
                "Origin": EVIL_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-access-code,content-type",
            },
        )
    except OSError as problem:
        return Check(name, FAIL, f"no answer ({problem})")
    for reply in (plain, preflight):
        found = judge_cors(reply.headers, EVIL_ORIGIN)
        if found:
            problems.append(found)
    return Check(
        name,
        FAIL if problems else PASS,
        "; ".join(problems) or "no cross-origin permission is sent",
    )


def check_headers(session: Session) -> list[Check]:
    results: list[Check] = []
    try:
        panel = session.request("GET", "/", headers={"Accept-Encoding": "gzip, zstd"})
        api = session.request("GET", "/api/healthz", headers={"Accept-Encoding": "gzip, br, zstd"})
    except OSError as problem:
        return [Check("security headers", FAIL, f"no answer ({problem})")]
    wrong = judge_headers(panel.headers, over_tls=session.target.tls)
    results.append(
        Check(
            "security headers on the panel",
            FAIL if wrong else PASS,
            "; ".join(wrong) or "all present",
        )
    )
    api_wrong = [
        w
        for w in judge_headers(api.headers, over_tls=session.target.tls)
        if "CSP" not in w and "content-security" not in w
    ]
    results.append(
        Check(
            "security headers on the API",
            FAIL if api_wrong else PASS,
            "; ".join(api_wrong) or "all present",
        )
    )
    encoded = api.headers.get("content-encoding")
    results.append(
        Check(
            "API is not compressed",
            FAIL if encoded else PASS,
            f"Content-Encoding: {encoded}"
            if encoded
            else "no content encoding (event streams stay unbuffered)",
        )
    )
    return results


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def check_ports(session: Session) -> list[Check]:
    host = session.target.host
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = dict(
            zip(CLOSED_PORTS, pool.map(lambda p: port_open(host, p), CLOSED_PORTS), strict=True)
        )
        ssh_open = port_open(host, SSH_PORT)
    opened = [port for port, is_open in states.items() if is_open]
    where = (
        "from here (a local rehearsal is not the internet)"
        if session.target.rehearsal
        else "from here"
    )
    results = [
        Check(
            "no Docker, API, panel or admin port reachable",
            FAIL if opened else PASS,
            f"open {where}: {opened}" if opened else f"closed {where}: {list(CLOSED_PORTS)}",
        )
    ]
    results.append(
        Check(
            "ssh reachability (info)",
            WARN if ssh_open else PASS,
            "port 22 answers from here: fine only if this is your allowed IP; check the Vultr "
            "firewall from another network"
            if ssh_open
            else "port 22 does not answer from here",
        )
    )
    return results


def check_no_leaks(session: Session) -> Check:
    problems = find_leaks(session.seen, session.code)
    return Check(
        "no secret in any response",
        FAIL if problems else PASS,
        "; ".join(problems[:5])
        or f"{len(session.seen)} responses scanned for the code and key shapes",
    )


# --- running everything ----------------------------------------------------------------


def run_checks(session: Session) -> list[Check]:
    results: list[Check] = [check_tls(session), check_http_redirect(session), check_health(session)]
    results.extend(check_ready(session))
    results.extend(check_code_refusals(session))
    results.append(check_kill_switch(session))
    results.extend(check_streaming(session))
    results.extend([check_oversize(session), check_oversize_sent(session), check_cors(session)])
    results.extend(check_headers(session))
    results.extend(check_ports(session))
    results.append(check_no_leaks(session))
    return results


def read_code(path: Path) -> str:
    """The access code from a regular file of mode 600 that we own; never printed."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"verify_deploy: {path} is not a regular file")
    if info.st_uid != os.getuid():
        raise SystemExit(f"verify_deploy: {path} is not owned by you")
    if stat.S_IMODE(info.st_mode) not in (0o600, 0o400):
        raise SystemExit(f"verify_deploy: {path} must have mode 600 (chmod 600 {path})")
    code = path.read_text(encoding="utf-8").strip()
    if not code or any(ch.isspace() for ch in code):
        raise SystemExit("verify_deploy: the code file must hold the code on one line")
    return code


def target_from(url: str, *, rehearsal: bool) -> Target:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SystemExit(
            "verify_deploy: give a URL like https://host (or http://host with --rehearsal)"
        )
    tls = parts.scheme == "https"
    return Target(parts.hostname, parts.port or (443 if tls else 80), tls, rehearsal)


def render(results: list[Check]) -> str:
    width = max(len(check.name) for check in results)
    lines = [f"{check.status:<8} {check.name:<{width}}  {check.detail}" for check in results]
    counts = {
        status: sum(1 for c in results if c.status == status)
        for status in (PASS, FAIL, WARN, UNTESTED)
    }
    lines.append("")
    lines.append(
        f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[WARN]} warnings, "
        f"{counts[UNTESTED]} untested"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a deployed Sandcage from the outside.")
    parser.add_argument("base_url", help="https://host of the deployment")
    parser.add_argument("--code-file", type=Path, help="file (mode 600) holding the access code")
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="a local stack: accept a self-signed certificate (or plain http); the checks that "
        "need a real VM are reported UNTESTED",
    )
    args = parser.parse_args(argv)
    code = read_code(args.code_file) if args.code_file else None
    session = Session(target_from(args.base_url, rehearsal=args.rehearsal), code)
    results = run_checks(session)
    print(render(results))
    if any(check.status == WARN and "gVisor is NOT in use" in check.detail for check in results):
        print("\nWARNING: this deployment is NOT running sandboxes in gVisor.", file=sys.stderr)
    return 1 if any(check.status == FAIL for check in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
