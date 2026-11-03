"""The standard-library Docker client against a canned Unix-socket server (no Docker)."""

from __future__ import annotations

import json
import shutil
import socket
import struct
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from sandcage.sandbox.docker_api import (
    DockerError,
    DockerNotFoundError,
    DockerRefusedError,
    DockerUnavailableError,
    EngineClient,
    checked_id,
    checked_image,
    demux,
)

Handler = Callable[[bytes, socket.socket], None]


def frame(payload: bytes, stream: int = 1) -> bytes:
    return struct.pack(">BxxxI", stream, len(payload)) + payload


def reply(status: int, body: object = None, reason: str = "OK") -> bytes:
    data = b"" if body is None else json.dumps(body).encode()
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
    ).encode() + data


@pytest.fixture
def server() -> Iterator[tuple[str, list[bytes], list[Handler]]]:
    """A Unix-socket server that answers each connection with the next handler."""
    folder = Path(tempfile.mkdtemp(prefix="sc-"))
    path = str(folder / "d.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(8)
    requests: list[bytes] = []
    handlers: list[Handler] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                requests.append(data)
                if handlers:
                    handlers.pop(0)(data, conn)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"unix://{path}", requests, handlers
    finally:
        listener.close()
        shutil.rmtree(folder, ignore_errors=True)


def answer(raw: bytes) -> Handler:
    return lambda _request, conn: conn.sendall(raw)


def test_demux_reassembles_frames() -> None:
    chunks = [frame(b"hello ")[:5], frame(b"hello ")[5:] + frame(b"world", 2), b"\x01\x00"]
    assert list(demux(iter(chunks))) == [b"hello ", b"world", b"\x01\x00"]


@pytest.mark.parametrize("bad", ["../etc", "a/b", "", "x" * 200, "id?q=1"])
def test_ids_are_validated_before_reaching_a_url(bad: str) -> None:
    with pytest.raises(DockerError):
        checked_id(bad)


def test_image_names_are_validated() -> None:
    assert checked_image("sandcage-sandbox:latest") == "sandcage-sandbox:latest"
    for bad in ("../x", "UPPER", "a b"):
        with pytest.raises(DockerError):
            checked_image(bad)


def test_bad_docker_host_is_refused() -> None:
    with pytest.raises(DockerError):
        EngineClient("http://example.com")
    assert isinstance(EngineClient.from_env({"DOCKER_HOST": "tcp://proxy:2375"}), EngineClient)


def test_unreachable_docker_is_unavailable(tmp_path: Path) -> None:
    for url in (f"unix://{tmp_path}/missing.sock", "unix:///nonexistent/d.sock"):
        client = EngineClient(url, timeout=1)
        with pytest.raises(DockerUnavailableError):
            client.ping()
        with pytest.raises(DockerUnavailableError):
            client.exec_start("e1", timeout=1)


def test_status_codes_map_to_errors(server: tuple[str, list[bytes], list[Handler]]) -> None:
    url, requests, handlers = server
    client = EngineClient(url, timeout=2)
    handlers.append(answer(reply(404, {"message": "No such container: abc"})))
    with pytest.raises(DockerNotFoundError, match="No such container"):
        client.inspect_container("abc")
    handlers.append(answer(reply(403, {"message": "refused by the sandcage proxy: build"})))
    with pytest.raises(DockerRefusedError, match="refused by the sandcage proxy"):
        client.ping()
    handlers.append(answer(reply(500, {"message": "boom"})))
    with pytest.raises(DockerError, match="500: boom"):
        client.info()
    handlers.append(answer(reply(404)))
    assert client.image_exists("sandcage-sandbox:latest") is False
    assert requests[0].startswith(b"GET /v1.44/containers/abc/json HTTP/1.1")
    assert requests[-1].startswith(b"GET /v1.44/images/sandcage-sandbox:latest/json")


def test_create_and_list_send_the_right_requests(
    server: tuple[str, list[bytes], list[Handler]],
) -> None:
    url, requests, handlers = server
    client = EngineClient(url, timeout=2)
    handlers.append(answer(reply(201, {"Id": "abc123", "Warnings": []})))
    assert client.create_container({"Image": "x"}) == "abc123"
    handlers.append(answer(reply(200, [{"Id": "abc123", "Labels": {"sandcage": "sandbox"}}])))
    assert client.list_containers({"sandcage": "sandbox"})[0]["Id"] == "abc123"
    assert requests[0].startswith(b"POST /v1.44/containers/create HTTP/1.1")
    assert b"filters=%7B%22label%22%3A+%5B%22sandcage%3Dsandbox%22%5D%7D" in requests[1]
    assert b"all=1" in requests[1]


def test_exec_start_hijacks_and_demultiplexes(
    server: tuple[str, list[bytes], list[Handler]],
) -> None:
    url, requests, handlers = server

    def upgraded(_request: bytes, conn: socket.socket) -> None:
        conn.sendall(
            b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
            b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n" + frame(b"out\n") + frame(b"err\n", 2)
        )

    handlers.append(upgraded)
    stream = EngineClient(url, timeout=2).exec_start("e1", timeout=2)
    assert b"".join(stream) == b"out\nerr\n"
    assert b"Upgrade: tcp" in requests[0]


def test_exec_start_error_answers_raise(server: tuple[str, list[bytes], list[Handler]]) -> None:
    url, _, handlers = server
    handlers.append(answer(reply(404, {"message": "No such exec instance: e9"})))
    with pytest.raises(DockerNotFoundError, match="No such exec"):
        EngineClient(url, timeout=2).exec_start("e9", timeout=2)


def test_closing_a_stream_unblocks_its_reader(
    server: tuple[str, list[bytes], list[Handler]],
) -> None:
    url, _, handlers = server
    hold = threading.Event()

    def never_ends(_request: bytes, conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 101 UPGRADED\r\n\r\n" + frame(b"partial"))
        hold.wait(5)

    handlers.append(never_ends)
    stream = EngineClient(url, timeout=5).exec_start("e1", timeout=5)
    received: list[bytes] = []
    done = threading.Event()

    def read() -> None:
        received.extend(stream)
        done.set()

    threading.Thread(target=read, daemon=True).start()
    assert not done.wait(0.2)
    stream.close()
    assert done.wait(2)
    hold.set()
    assert received == [b"partial"]
