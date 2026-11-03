"""The proxy process against a fake Docker daemon: one request per connection, labels
checked upstream, full ids forwarded, refusals never reach Docker, streams leave nothing."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from sandcage.dockerproxy.policy import ProxyConfig
from sandcage.dockerproxy.server import DockerProxy
from sandcage.sandbox.docker_api import DockerRefusedError, EngineClient
from sandcage.sandbox.hardening import OwnerIdentity, hardened_config

SANDBOX_ID = "a" * 64
PLAIN_ID = "b" * 64
WORK = "/srv/sandcage/work"


def frame(payload: bytes) -> bytes:
    return struct.pack(">BxxxI", 1, len(payload)) + payload


def http(status: int, body: bytes = b"{}", reason: str = "OK") -> bytes:
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


@dataclass
class FakeDockerd:
    """A Unix-socket Docker stand-in that records every request it receives."""

    path: str
    requests: list[str] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    containers: ClassVar[dict[str, dict[str, object]]] = {
        "abc": {"Id": SANDBOX_ID, "Config": {"Labels": {"sandcage": "sandbox"}}},
        SANDBOX_ID: {"Id": SANDBOX_ID, "Config": {"Labels": {"sandcage": "sandbox"}}},
        "plain": {"Id": PLAIN_ID, "Config": {"Labels": {"app": "db"}}},
        PLAIN_ID: {"Id": PLAIN_ID, "Config": {"Labels": {"app": "db"}}},
    }
    execs: ClassVar[dict[str, str]] = {"e1": SANDBOX_ID, "e2": PLAIN_ID}

    def answer(self, line: str, conn: socket.socket) -> None:
        _method, target, _version = line.split(" ")
        path = target.split("?")[0].removeprefix("/v1.44")
        parts = path.strip("/").split("/")
        if path == "/_ping":
            conn.sendall(http(200, b"OK"))
        elif parts[0] == "containers" and len(parts) == 3 and parts[2] == "json":
            info = self.containers.get(parts[1])
            conn.sendall(
                http(200, json.dumps(info).encode()) if info else http(404, b'{"message":"no"}')
            )
        elif parts[0] == "exec" and parts[2] == "json":
            container = self.execs.get(parts[1])
            body = json.dumps({"ContainerID": container, "Running": False, "ExitCode": 0})
            conn.sendall(http(200, body.encode()) if container else http(404))
        elif parts[0] == "exec" and parts[2] == "start":
            conn.sendall(b"HTTP/1.1 101 UPGRADED\r\n\r\n" + frame(b"hello from exec\n"))
        elif path == "/containers/create":
            conn.sendall(http(201, json.dumps({"Id": SANDBOX_ID}).encode()))
        else:
            conn.sendall(http(200))

    def serve(self, listener: socket.socket) -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                length = next(
                    (
                        int(v)
                        for k, _, v in (h.partition(": ") for h in lines)
                        if k.lower() == "content-length"
                    ),
                    0,
                )
                while len(rest) < length:
                    rest += conn.recv(65536)
                self.requests.append(head.decode())
                self.bodies.append(rest)
                self.answer(lines[0], conn)


@dataclass
class Running:
    proxy: DockerProxy
    loop: asyncio.AbstractEventLoop
    server: asyncio.Server
    port: int

    def tasks(self) -> int:
        async def count() -> int:
            return len(asyncio.all_tasks()) - 1

        return asyncio.run_coroutine_threadsafe(count(), self.loop).result(5)


@pytest.fixture
def dockerd() -> Iterator[FakeDockerd]:
    folder = Path(tempfile.mkdtemp(prefix="sc-"))
    path = str(folder / "docker.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(16)
    fake = FakeDockerd(path)
    threading.Thread(target=fake.serve, args=(listener,), daemon=True).start()
    try:
        yield fake
    finally:
        listener.close()
        shutil.rmtree(folder, ignore_errors=True)


def start_proxy(socket_path: str) -> Running:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    proxy = DockerProxy(ProxyConfig("sandcage-sandbox:latest", WORK), socket_path, io_timeout=2)
    server = asyncio.run_coroutine_threadsafe(proxy.start("127.0.0.1", 0), loop).result(5)
    return Running(proxy, loop, server, server.sockets[0].getsockname()[1])


def stop_proxy(running: Running) -> None:
    async def close() -> None:
        running.server.close()
        await running.server.wait_closed()

    asyncio.run_coroutine_threadsafe(close(), running.loop).result(5)
    running.loop.call_soon_threadsafe(running.loop.stop)


@pytest.fixture
def proxy(dockerd: FakeDockerd) -> Iterator[Running]:
    running = start_proxy(dockerd.path)
    try:
        yield running
    finally:
        stop_proxy(running)


def raw(running: Running, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as conn:
        conn.sendall(data)
        received = b""
        while chunk := conn.recv(65536):
            received += chunk
    return received


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def wait_until(condition: Callable[[], bool], seconds: float = 2.0) -> bool:
    """A bounded wait for the proxy's own asynchronous cleanup to finish."""
    give_up = time.monotonic() + seconds
    while time.monotonic() < give_up:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_an_allowed_request_is_forwarded_with_connection_close(
    proxy: Running, dockerd: FakeDockerd
) -> None:
    response = raw(proxy, b"GET /v1.44/_ping HTTP/1.1\r\nHost: x\r\nX-Evil: 1\r\n\r\n")
    assert status_of(response) == 200
    assert response.endswith(b"OK")
    (request,) = dockerd.requests
    assert request.startswith("GET /v1.44/_ping HTTP/1.1")
    assert "Connection: close" in request
    assert "X-Evil" not in request


def test_a_refused_request_never_reaches_docker(proxy: Running, dockerd: FakeDockerd) -> None:
    response = raw(proxy, b"POST /v1.44/build HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
    assert status_of(response) == 403
    assert b"refused by the sandcage proxy" in response
    assert dockerd.requests == []


def test_only_one_request_per_connection(proxy: Running, dockerd: FakeDockerd) -> None:
    pipelined = (
        b"GET /_ping HTTP/1.1\r\nHost: x\r\n\r\n"
        b"POST /v1.44/containers/plain/exec HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"
    )
    response = raw(proxy, pipelined)
    assert status_of(response) == 200
    assert response.count(b"HTTP/1.1") == 1
    assert [r.split("\r\n")[0] for r in dockerd.requests] == ["GET /_ping HTTP/1.1"]


def test_labels_are_checked_upstream_and_the_full_id_is_forwarded(
    proxy: Running, dockerd: FakeDockerd
) -> None:
    ok = raw(proxy, b"POST /v1.44/containers/abc/start HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
    assert status_of(ok) == 200
    assert [r.split("\r\n")[0] for r in dockerd.requests] == [
        "GET /v1.44/containers/abc/json HTTP/1.1",
        f"POST /v1.44/containers/{SANDBOX_ID}/start HTTP/1.1",
    ]


def test_a_container_without_the_sandbox_label_is_refused(
    proxy: Running, dockerd: FakeDockerd
) -> None:
    body = json.dumps({"Cmd": ["id"], "User": "65534:65534"}).encode()
    request = (
        f"POST /v1.44/containers/plain/exec HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    response = raw(proxy, request)
    assert status_of(response) == 403
    assert b"not a sandbox container" in response
    assert [r.split("\r\n")[0] for r in dockerd.requests] == [
        "GET /v1.44/containers/plain/json HTTP/1.1"
    ]
    missing = raw(proxy, b"GET /containers/nothing/json HTTP/1.1\r\n\r\n")
    assert status_of(missing) == 404


def test_exec_needs_a_sandbox_container_and_streams_are_piped(
    proxy: Running, dockerd: FakeDockerd
) -> None:
    client = EngineClient(f"tcp://127.0.0.1:{proxy.port}", timeout=5)
    with pytest.raises(DockerRefusedError, match="not a sandbox container"):
        client.exec_start("e2", timeout=5)
    stream = client.exec_start("e1", timeout=5)
    assert b"".join(stream) == b"hello from exec\n"
    started = [r for r in dockerd.requests if "/exec/e1/start" in r.split("\r\n")[0]]
    assert len(started) == 1
    assert "Upgrade: tcp" in started[0]
    assert wait_until(lambda: proxy.tasks() == 0)  # nothing left running


def test_a_hardened_create_sent_chunked_is_forwarded_whole(
    proxy: Running, dockerd: FakeDockerd
) -> None:
    config = hardened_config(
        command=["sleep", "infinity"],
        identity=OwnerIdentity("api", 1, "x"),
        workspace=Path(f"{WORK}/sandcage-ab12/workspace"),
        runtime=None,
    )
    body = json.dumps(config).encode()
    half = len(body) // 2
    chunked = (
        f"{half:x}\r\n".encode()
        + body[:half]
        + b"\r\n"
        + f"{len(body) - half:x}\r\n".encode()
        + body[half:]
        + b"\r\n0\r\n\r\n"
    )
    request = (
        b"POST /v1.44/containers/create HTTP/1.1\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n" + chunked
    )
    response = raw(proxy, request)
    assert status_of(response) == 201
    assert json.loads(dockerd.bodies[-1]) == config
    assert f"Content-Length: {len(body)}" in dockerd.requests[-1]


@pytest.mark.parametrize(
    ("request_bytes", "status"),
    [
        (b"garbage\r\n\r\n", 400),
        (b"GET /_ping HTTP/9.9\r\n\r\n", 400),
        (b"GET /_ping HTTP/1.1\r\n bad-fold: x\r\n\r\n", 400),
        (b"POST /containers/create HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n", 413),
        (
            b"POST /containers/create HTTP/1.1\r\nContent-Length: 2\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n{}",
            400,
        ),
        (b"POST /containers/create HTTP/1.1\r\nTransfer-Encoding: gzip\r\n\r\n", 400),
        (b"POST /containers/create HTTP/1.1\r\nContent-Length: x\r\n\r\n", 400),
        (b"GET /" + b"a" * 70_000 + b" HTTP/1.1\r\n\r\n", 400),
    ],
)
def test_malformed_requests_are_rejected(
    proxy: Running, dockerd: FakeDockerd, request_bytes: bytes, status: int
) -> None:
    assert status_of(raw(proxy, request_bytes)) == status
    assert dockerd.requests == []


def test_docker_unreachable_is_502(tmp_path: Path) -> None:
    running = start_proxy(str(tmp_path / "no-docker.sock"))
    try:
        assert status_of(raw(running, b"GET /_ping HTTP/1.1\r\n\r\n")) == 502
        assert status_of(raw(running, b"GET /containers/abc/json HTTP/1.1\r\n\r\n")) == 502
    finally:
        stop_proxy(running)


def test_main_refuses_to_start_without_a_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandcage.dockerproxy import server

    monkeypatch.delenv("SANDCAGE_WORK_DIR", raising=False)
    assert server.main() == 2
    monkeypatch.setenv("SANDCAGE_WORK_DIR", "relative")
    assert server.main() == 2
