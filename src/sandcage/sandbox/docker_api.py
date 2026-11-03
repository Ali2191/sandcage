"""The few Docker Engine API calls Sandcage makes, behind a small protocol.

:class:`DockerAPI` is what the container, the runtime probe and the reaper depend on, so
tests drive them with a scripted fake and no Docker. :class:`EngineClient` implements it
with the standard library over a Unix socket or TCP (the Docker socket proxy). Every call
opens its own connection and has a timeout; exec output is read from the hijacked
connection and demultiplexed. Ids and image names are validated before they reach a URL.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import socket
import struct
from collections.abc import Iterator, Mapping, Sequence
from typing import Final, Protocol
from urllib.parse import quote, urlencode, urlsplit

API_VERSION: Final = "v1.44"
DEFAULT_HOST: Final = "unix:///var/run/docker.sock"
CALL_TIMEOUT: Final = 30.0
MAX_RESPONSE: Final = 8 * 1024 * 1024
_HEAD_LIMIT: Final = 64 * 1024
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_IMAGE: Final = re.compile(r"[a-z0-9][a-z0-9._/:@-]{0,254}")


class DockerError(Exception):
    """A Docker call failed. Messages carry Docker's own message, never host secrets."""


class DockerUnavailableError(DockerError):
    """Docker (or the proxy) cannot be reached at all."""


class DockerNotFoundError(DockerError):
    pass


class DockerRefusedError(DockerError):
    """The Docker socket proxy refused the request (HTTP 403)."""


class ExecStream(Protocol):
    """The output of one exec, demultiplexed (stdout and stderr interleaved)."""

    def __iter__(self) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class DockerAPI(Protocol):
    def ping(self) -> None: ...

    def info(self) -> Mapping[str, object]: ...

    def image_exists(self, image: str) -> bool: ...

    def create_container(self, config: Mapping[str, object]) -> str: ...

    def start_container(self, container_id: str) -> None: ...

    def wait_container(self, container_id: str, *, timeout: float) -> int: ...

    def container_logs(self, container_id: str) -> bytes: ...

    def inspect_container(self, container_id: str) -> Mapping[str, object]: ...

    def remove_container(self, container_id: str) -> None: ...

    def list_containers(self, labels: Mapping[str, str]) -> list[Mapping[str, object]]: ...

    def exec_create(self, container_id: str, command: Sequence[str], *, user: str) -> str: ...

    def exec_start(self, exec_id: str, *, timeout: float) -> ExecStream: ...

    def exec_inspect(self, exec_id: str) -> Mapping[str, object]: ...


def checked_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise DockerError("invalid container or exec id")
    return value


def checked_image(value: str) -> str:
    if not _IMAGE.fullmatch(value) or ".." in value:
        raise DockerError("invalid image name")
    return value


# --- demultiplexing ------------------------------------------------------------------


def demux(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Payloads of Docker's multiplexed stream (8-byte header: stream, 0, 0, 0, size)."""
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        while len(buffer) >= 8:
            size = struct.unpack(">I", bytes(buffer[4:8]))[0]
            if len(buffer) < 8 + size:
                break
            payload = bytes(buffer[8 : 8 + size])
            del buffer[: 8 + size]
            if payload:
                yield payload
    if buffer:
        yield bytes(buffer)  # a trailing partial frame: keep what arrived


class _SocketStream:
    """An exec's hijacked connection. ``close`` shuts it down, which also unblocks a
    reader thread waiting in ``recv``."""

    def __init__(self, sock: socket.socket, leftover: bytes) -> None:
        self._sock = sock
        self._leftover = leftover
        self._closed = False

    def _raw(self) -> Iterator[bytes]:
        if self._leftover:
            yield self._leftover
        while not self.is_closed():
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                if self.is_closed():  # closed from another thread to stop the reader
                    return
                raise
            if not chunk:
                return
            yield chunk

    def is_closed(self) -> bool:
        return self._closed

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from demux(self._raw())
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):  # already disconnected by the other side
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()


# --- the client ----------------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class EngineClient:
    """Standard-library Docker Engine API client (``unix://path`` or ``tcp://host:port``)."""

    def __init__(self, base_url: str = DEFAULT_HOST, *, timeout: float = CALL_TIMEOUT) -> None:
        parts = urlsplit(base_url)
        if parts.scheme == "unix" and parts.path:
            self._unix: str | None = parts.path
            self._tcp: tuple[str, int] | None = None
        elif parts.scheme == "tcp" and parts.hostname and parts.port:
            self._unix = None
            self._tcp = (parts.hostname, parts.port)
        else:
            raise DockerError("DOCKER_HOST must be unix:///path or tcp://host:port")
        self._timeout = timeout

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EngineClient:
        env = os.environ if environ is None else environ
        return cls(env.get("DOCKER_HOST") or DEFAULT_HOST)

    # --- plumbing ------------------------------------------------------------------

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self._unix is not None:
            return _UnixConnection(self._unix, timeout)
        host, port = self._tcp_address()
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _tcp_address(self) -> tuple[str, int]:
        if self._tcp is None:
            raise DockerError("no Docker address is configured")
        return self._tcp

    def _raw_socket(self, timeout: float) -> socket.socket:
        try:
            if self._unix is not None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                try:
                    sock.connect(self._unix)
                except OSError:
                    sock.close()
                    raise
                return sock
            return socket.create_connection(self._tcp_address(), timeout=timeout)
        except OSError as problem:
            raise DockerUnavailableError(
                f"cannot reach Docker ({type(problem).__name__})"
            ) from None

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: object = None,
        timeout: float | None = None,
    ) -> bytes:
        target = f"/{API_VERSION}{path}"
        if query:
            target += "?" + urlencode(query)
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Host": "docker", "Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection = self._connection(timeout or self._timeout)
        try:
            try:
                connection.connect()
            except OSError as problem:
                raise DockerUnavailableError(
                    f"cannot reach Docker ({type(problem).__name__})"
                ) from None
            try:
                connection.request(method, target, body=payload, headers=headers)
                response = connection.getresponse()
                data = response.read(MAX_RESPONSE + 1)
            except TimeoutError:
                raise DockerError(f"Docker did not answer {method} {path} in time") from None
            except (ConnectionError, FileNotFoundError) as problem:
                raise DockerUnavailableError(
                    f"cannot reach Docker ({type(problem).__name__})"
                ) from None
            except (OSError, http.client.HTTPException) as problem:
                raise DockerError(f"Docker connection failed ({type(problem).__name__})") from None
        finally:
            connection.close()
        if len(data) > MAX_RESPONSE:
            raise DockerError("Docker's answer was too large")
        _raise_for(response.status, data)
        return data

    def _json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: object = None,
        timeout: float | None = None,
    ) -> object:
        data = self._call(method, path, query=query, body=body, timeout=timeout)
        try:
            return json.loads(data) if data else None
        except ValueError:
            raise DockerError("Docker sent an answer that is not JSON") from None

    def _mapping(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        value = self._json(method, path, body=body, timeout=timeout)
        if not isinstance(value, dict):
            raise DockerError("Docker sent an unexpected answer")
        return value

    # --- the API -------------------------------------------------------------------

    def ping(self) -> None:
        self._call("GET", "/_ping")

    def info(self) -> Mapping[str, object]:
        return self._mapping("GET", "/info")

    def image_exists(self, image: str) -> bool:
        try:
            self._call("GET", f"/images/{quote(checked_image(image), safe='/:@')}/json")
        except DockerNotFoundError:
            return False
        return True

    def create_container(self, config: Mapping[str, object]) -> str:
        created = self._mapping("POST", "/containers/create", body=dict(config))
        container_id = created.get("Id")
        if not isinstance(container_id, str):
            raise DockerError("Docker did not return a container id")
        return checked_id(container_id)

    def start_container(self, container_id: str) -> None:
        self._call("POST", f"/containers/{checked_id(container_id)}/start")

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        result = self._mapping(
            "POST", f"/containers/{checked_id(container_id)}/wait", timeout=timeout
        )
        code = result.get("StatusCode")
        if not isinstance(code, int):
            raise DockerError("Docker did not return an exit status")
        return code

    def container_logs(self, container_id: str) -> bytes:
        data = self._call(
            "GET",
            f"/containers/{checked_id(container_id)}/logs",
            query={"stdout": "1", "stderr": "1"},
        )
        return b"".join(demux(iter([data])))

    def inspect_container(self, container_id: str) -> Mapping[str, object]:
        return self._mapping("GET", f"/containers/{checked_id(container_id)}/json")

    def remove_container(self, container_id: str) -> None:
        self._call(
            "DELETE", f"/containers/{checked_id(container_id)}", query={"force": "1", "v": "1"}
        )

    def list_containers(self, labels: Mapping[str, str]) -> list[Mapping[str, object]]:
        filters = json.dumps({"label": [f"{key}={value}" for key, value in labels.items()]})
        value = self._json("GET", "/containers/json", query={"all": "1", "filters": filters})
        if not isinstance(value, list):
            raise DockerError("Docker sent an unexpected container list")
        return [item for item in value if isinstance(item, dict)]

    def exec_create(self, container_id: str, command: Sequence[str], *, user: str) -> str:
        created = self._mapping(
            "POST",
            f"/containers/{checked_id(container_id)}/exec",
            body={
                "Cmd": list(command),
                "User": user,
                "AttachStdin": False,
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": False,
                "WorkingDir": "/workspace",
            },
        )
        exec_id = created.get("Id")
        if not isinstance(exec_id, str):
            raise DockerError("Docker did not return an exec id")
        return checked_id(exec_id)

    def exec_start(self, exec_id: str, *, timeout: float) -> ExecStream:
        body = json.dumps({"Detach": False, "Tty": False}).encode()
        request = (
            f"POST /{API_VERSION}/exec/{checked_id(exec_id)}/start HTTP/1.1\r\n"
            "Host: docker\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n"
        ).encode() + body
        sock = self._raw_socket(timeout)
        try:
            sock.sendall(request)
            head, leftover = _read_head(sock)
            status = _status_of(head)
            if status not in {101, 200}:
                rest = leftover + _drain(sock)
                _raise_for(status, _body_after(head, rest))
                raise DockerError(f"unexpected exec start answer {status}")
        except BaseException:
            sock.close()
            raise
        return _SocketStream(sock, leftover)

    def exec_inspect(self, exec_id: str) -> Mapping[str, object]:
        return self._mapping("GET", f"/exec/{checked_id(exec_id)}/json")


def _raise_for(status: int, data: bytes) -> None:
    if 200 <= status < 300 or status == 304:
        return
    try:
        message = str(json.loads(data).get("message", "")) if data else ""
    except (ValueError, AttributeError):
        message = data[:200].decode("utf-8", errors="replace")
    message = message[:500] or f"HTTP {status}"
    if status == 404:
        raise DockerNotFoundError(message)
    if status == 403:
        raise DockerRefusedError(message)
    raise DockerError(f"Docker answered {status}: {message}")


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) > _HEAD_LIMIT:
            raise DockerError("Docker sent an oversized response head")
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            raise DockerError("Docker did not answer exec start in time") from None
        if not chunk:
            raise DockerError("Docker closed the connection during exec start")
        data.extend(chunk)
    head, _, rest = bytes(data).partition(b"\r\n\r\n")
    return head, rest


def _status_of(head: bytes) -> int:
    try:
        return int(head.split(b"\r\n", 1)[0].split(b" ")[1])
    except (IndexError, ValueError):
        raise DockerError("Docker sent a malformed response") from None


def _drain(sock: socket.socket, limit: int = 65536) -> bytes:
    data = bytearray()
    try:
        while len(data) < limit and (chunk := sock.recv(4096)):
            data.extend(chunk)
    except OSError:
        pass  # whatever arrived is enough for an error message
    return bytes(data)


def _body_after(head: bytes, rest: bytes) -> bytes:
    """The body of a non-upgraded answer (chunked bodies keep only the first chunk)."""
    if b"transfer-encoding: chunked" in head.lower():
        size_line, _, remainder = rest.partition(b"\r\n")
        try:
            size = int(size_line.strip() or b"0", 16)
        except ValueError:
            return rest
        return remainder[:size]
    return rest
