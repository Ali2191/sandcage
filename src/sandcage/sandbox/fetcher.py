"""``http_get``, executed in the API process because the sandbox has no network.

Only reached after the gate allowed the URL. For every hop (the first request and each
redirect, followed by hand):

1. the URL is re-checked against the run's policy (``check``) and must be allowed;
2. only ``http``/``https`` on the default ports, no user info in the URL;
3. the host is resolved once, and *every* resolved address must be public
   (:func:`sandcage.sandbox.addresses.address_problem`);
4. the connection goes to that pinned address, with the original name in the Host header
   and as the TLS server name, so DNS rebinding cannot switch the address.

One deadline covers the whole fetch (resolution, every hop and every read), the body is
capped, and environment proxies are never used. Errors come back as values, not raised.
"""

from __future__ import annotations

import codecs
import http.client
import queue
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urljoin, urlsplit

from sandcage.masking import mask
from sandcage.sandbox.addresses import address_problem

FETCH_TIMEOUT: Final = 10.0
MAX_BYTES: Final = 50_000
MAX_REDIRECTS: Final = 5
DEFAULT_PORTS: Final = {"http": 80, "https": 443}
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
USER_AGENT: Final = "Sandcage-fetcher/0.1"
_CHUNK: Final = 8192
_CHARSET: Final = re.compile(r"charset=\"?([A-Za-z0-9._-]+)", re.IGNORECASE)


class FetchProblemError(Exception):
    """A hop failed; the message is safe to show (masked, no host paths)."""


@dataclass(frozen=True)
class Request:
    scheme: str
    host: str
    port: int
    target: str  # path and query
    address: str  # the pinned, checked address to connect to


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]  # lower-case names, first value
    body: bytes
    truncated: bool


@dataclass(frozen=True)
class Hop:
    """One request on the way, for the run's egress record."""

    url: str
    host: str
    address: str
    status: int
    bytes_received: int


@dataclass(frozen=True)
class Page:
    url: str
    status: int
    content_type: str
    text: str
    truncated: bool
    hops: tuple[Hop, ...]


@dataclass(frozen=True)
class FetchFailure:
    reason: str
    hops: tuple[Hop, ...] = ()


UrlCheck = Callable[[str], str | None]
"""Returns None when the policy allows fetching the URL, else the refusal reason."""


class Resolver(Protocol):
    def __call__(self, host: str, port: int, *, timeout: float) -> Sequence[str]: ...


class Transport(Protocol):
    def __call__(self, request: Request, *, deadline: float, max_bytes: int) -> RawResponse: ...


# --- the fetcher ---------------------------------------------------------------------


class Fetcher:
    def __init__(
        self,
        check: UrlCheck,
        *,
        resolver: Resolver | None = None,
        transport: Transport | None = None,
        timeout: float = FETCH_TIMEOUT,
        max_bytes: int = MAX_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._resolver: Resolver = resolver or system_resolver
        self._transport: Transport = transport or http_transport
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._clock = clock

    def fetch(self, url: str) -> Page | FetchFailure:
        deadline = self._clock() + self.timeout
        hops: list[Hop] = []
        seen = {url}
        current = url
        for _ in range(self.max_redirects + 1):
            try:
                request = self._prepare(current, deadline)
                response = self._send(request, deadline)
            except FetchProblemError as problem:
                return FetchFailure(str(problem), tuple(hops))
            hops.append(
                Hop(
                    mask(current),
                    request.host,
                    request.address,
                    response.status,
                    len(response.body),
                )
            )
            location = response.headers.get("location")
            if response.status in REDIRECT_STATUSES and location:
                current = urljoin(current, location.strip())
                if current in seen:
                    return FetchFailure(
                        "the redirects loop back to an earlier address", tuple(hops)
                    )
                seen.add(current)
                continue
            return _page(current, response, tuple(hops))
        return FetchFailure(f"more than {self.max_redirects} redirects", tuple(hops))

    def _prepare(self, url: str, deadline: float) -> Request:
        shown = mask(url)
        refusal = self._check(url)
        if refusal is not None:
            raise FetchProblemError(f"fetching {shown} is not allowed: {refusal}")
        scheme, host, port, target = _parse(url, shown)
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise FetchProblemError("the fetch timed out")
        try:
            addresses = list(dict.fromkeys(self._resolver(host, port, timeout=remaining)))
        except (OSError, TimeoutError):
            raise FetchProblemError(f"{host} could not be resolved") from None
        if not addresses:
            raise FetchProblemError(f"{host} could not be resolved")
        for address in addresses:
            problem = address_problem(address)
            if problem is not None:
                raise FetchProblemError(
                    f"{host} resolves to {address}, a {problem}, which is never contacted"
                )
        return Request(scheme, host, port, target, addresses[0])

    def _send(self, request: Request, deadline: float) -> RawResponse:
        try:
            return self._transport(request, deadline=deadline, max_bytes=self.max_bytes)
        except TimeoutError:
            raise FetchProblemError("the fetch timed out") from None
        except ssl.SSLError as problem:
            raise FetchProblemError(f"TLS failed for {request.host}: {problem.reason}") from None
        except (OSError, http.client.HTTPException) as problem:
            kind = type(problem).__name__
            raise FetchProblemError(f"network error talking to {request.host} ({kind})") from None


def _parse(url: str, shown: str) -> tuple[str, str, int, str]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchProblemError(f"{shown} is not a valid address") from None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise FetchProblemError("only http and https addresses can be fetched")
    if parts.username is not None or parts.password is not None:
        raise FetchProblemError("addresses with a user name or password are not fetched")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise FetchProblemError(f"{shown} has no host name")
    try:
        host.encode("idna")
    except UnicodeError:
        raise FetchProblemError(f"{shown} has an invalid host name") from None
    if port is not None and port != DEFAULT_PORTS[scheme]:
        raise FetchProblemError(f"only the default {scheme} port can be used")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return scheme, host, DEFAULT_PORTS[scheme], target


def _page(url: str, response: RawResponse, hops: tuple[Hop, ...]) -> Page:
    content_type = response.headers.get("content-type", "")
    match = _CHARSET.search(content_type)
    encoding = "utf-8"
    if match:
        try:
            encoding = codecs.lookup(match.group(1)).name
        except LookupError:
            encoding = "utf-8"
    text = response.body.decode(encoding, errors="replace")
    return Page(mask(url), response.status, content_type, text, response.truncated, hops)


# --- the real resolver and transport -------------------------------------------------


def system_resolver(host: str, port: int, *, timeout: float) -> list[str]:
    """getaddrinfo with a timeout (it has none of its own), run on a helper thread."""
    results: queue.Queue[list[str] | OSError] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            results.put([str(info[4][0]) for info in infos])
        except OSError as problem:
            results.put(problem)

    threading.Thread(target=resolve, name="sandcage-resolve", daemon=True).start()
    try:
        outcome = results.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("name resolution timed out") from None
    if isinstance(outcome, OSError):
        raise outcome
    return outcome


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, host: str, port: int, address: str, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls = context

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def http_transport(request: Request, *, deadline: float, max_bytes: int) -> RawResponse:
    """GET over a connection to the pinned address; reads at most ``max_bytes`` + 1."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("the fetch timed out")
    connection: http.client.HTTPConnection
    if request.scheme == "https":
        context = ssl.create_default_context()
        connection = _PinnedHTTPSConnection(
            request.host, request.port, request.address, remaining, context
        )
    else:
        connection = _PinnedHTTPConnection(request.host, request.port, request.address, remaining)
    try:
        connection.request(
            "GET",
            request.target,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.5",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        headers: dict[str, str] = {}
        for name, value in response.getheaders():
            headers.setdefault(name.lower(), value)
        body = bytearray()
        while len(body) <= max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the fetch timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read(min(_CHUNK, max_bytes + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        truncated = len(body) > max_bytes
        return RawResponse(response.status, headers, bytes(body[:max_bytes]), truncated)
    finally:
        connection.close()
