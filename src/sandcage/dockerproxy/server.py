"""The filtering proxy process (standard library only, asyncio).

Listens on TCP for the API, forwards to the Docker socket.

- **One request per client connection.** The head (at most 64 KB) and the body (at most
  1 MiB, by Content-Length or chunked) are read completely, judged by
  :func:`check_request`, and forwarded with ``Connection: close``; nothing after that
  first request is ever read or forwarded, so a second request cannot ride along.
- **Labels are checked upstream.** For a request naming a container (or an exec, through
  its container) the proxy asks Docker itself whether the container carries the sandbox
  label, then forwards with the container's *full id*, so a name cannot be swapped for
  another container between the check and the use.
- Refusals answer 403 with the reason (and are logged); malformed requests 400; a body
  too large 413; Docker unreachable 502. Reads and upstream checks time out after 30 s.
- ``exec start`` upgrades to a raw stream: Docker's output is piped to the client until
  Docker is done; the client side is only watched for closing (exec has no stdin), and
  every task and socket is closed afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sandcage.dockerproxy.policy import (
    SANDBOX_LABEL,
    Decision,
    ProxyConfig,
    ProxyConfigError,
    check_request,
)

HEAD_LIMIT: Final = 64 * 1024
BODY_LIMIT: Final = 1024 * 1024
UPSTREAM_LIMIT: Final = 8 * 1024 * 1024
IO_TIMEOUT: Final = 30.0
STREAM_IDLE: Final = 120.0
API_PREFIX: Final = "/v1.44"
_KEEP_HEADERS: Final = frozenset({"content-type", "accept", "user-agent"})
_METHOD: Final = re.compile(r"[A-Z]{1,10}")
_EXEC_START: Final = re.compile(r"/exec/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/start")
_REASONS: Final = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    413: "Payload Too Large",
    502: "Bad Gateway",
}

log = logging.getLogger("sandcage.dockerproxy")


class BadRequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Request:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key == name:
                return value
        return None


@dataclass(frozen=True)
class UpstreamAnswer:
    status: int
    body: bytes


class DockerProxy:
    def __init__(
        self,
        config: ProxyConfig,
        docker_socket: str,
        *,
        io_timeout: float = IO_TIMEOUT,
        stream_idle: float = STREAM_IDLE,
    ) -> None:
        self.config = config
        self._socket = docker_socket
        self._timeout = io_timeout
        self._idle = stream_idle

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._handle, host, port, limit=HEAD_LIMIT)

    # --- one connection, one request -------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve_one(reader, writer)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), self._timeout)
            except (ConnectionError, TimeoutError):
                writer.transport.abort()  # a client that never lets go loses its socket

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader)
        except BadRequestError as problem:
            await _reply(writer, problem.status, problem.message)
            return
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return
        decision = check_request(request.method, request.target, request.body, self.config)
        if not decision.allowed:
            log.warning(
                "refused %s %s: %s", request.method, _path_only(request.target), decision.reason
            )
            await _reply(writer, 403, f"refused by the sandcage proxy: {decision.reason}")
            return
        try:
            target = request.target
            if decision.container is not None or decision.exec_id is not None:
                confirmed = await self._confirm_sandbox(decision, request.target)
                if isinstance(confirmed, BadRequestError):
                    if confirmed.status == 403:
                        log.warning(
                            "refused %s %s: %s",
                            request.method,
                            _path_only(request.target),
                            confirmed.message,
                        )
                    await _reply(writer, confirmed.status, confirmed.message)
                    return
                target = confirmed
            await self._forward(request, target, decision, reader, writer)
        except (OSError, TimeoutError) as problem:
            log.error("could not complete the request through Docker: %s", type(problem).__name__)
            with contextlib.suppress(ConnectionError):
                await _reply(writer, 502, "the sandcage proxy cannot reach Docker")

    # --- reading the client's request ------------------------------------------------

    async def _read_request(self, reader: asyncio.StreamReader) -> Request:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._timeout)
        except asyncio.LimitOverrunError:
            raise BadRequestError(400, "the request head is too large") from None
        lines = head[:-4].decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if (
            len(parts) != 3
            or not _METHOD.fullmatch(parts[0])
            or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}
        ):
            raise BadRequestError(400, "malformed request line")
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if not colon or not name or name != name.strip() or line[:1] in {" ", "\t"}:
                raise BadRequestError(400, "malformed header")
            headers.append((name.lower(), value.strip()))
        body = await self._read_body(reader, headers)
        return Request(parts[0], parts[1], tuple(headers), body)

    async def _read_body(
        self, reader: asyncio.StreamReader, headers: Sequence[tuple[str, str]]
    ) -> bytes:
        lengths = [value for key, value in headers if key == "content-length"]
        encodings = [value.lower() for key, value in headers if key == "transfer-encoding"]
        if lengths and encodings:
            raise BadRequestError(400, "both Content-Length and Transfer-Encoding")
        if len(lengths) > 1 or len(encodings) > 1:
            raise BadRequestError(400, "repeated length headers")
        if encodings:
            if encodings[0] != "chunked":
                raise BadRequestError(400, "unsupported transfer encoding")
            return await self._read_chunked(reader)
        if not lengths:
            return b""
        if not lengths[0].isdigit():
            raise BadRequestError(400, "malformed Content-Length")
        size = int(lengths[0])
        if size > BODY_LIMIT:
            raise BadRequestError(413, "the request body is too large")
        return await asyncio.wait_for(reader.readexactly(size), self._timeout)

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), self._timeout)
            size_text = line[:-2].split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                raise BadRequestError(400, "malformed chunk size") from None
            if size == 0:
                while await asyncio.wait_for(reader.readuntil(b"\r\n"), self._timeout) != b"\r\n":
                    pass  # trailer lines are read and dropped
                return bytes(body)
            if size < 0 or len(body) + size > BODY_LIMIT:
                raise BadRequestError(413, "the request body is too large")
            chunk = await asyncio.wait_for(reader.readexactly(size + 2), self._timeout)
            if chunk[-2:] != b"\r\n":
                raise BadRequestError(400, "malformed chunk")
            body.extend(chunk[:-2])

    # --- the upstream label check ----------------------------------------------------

    async def _confirm_sandbox(self, decision: Decision, target: str) -> str | BadRequestError:
        """The target to forward (with the container's full id), or the refusal."""
        container = decision.container
        if decision.exec_id is not None:
            answer = await self._upstream_get(f"/exec/{decision.exec_id}/json")
            if answer.status == 404:
                return BadRequestError(404, "No such exec instance")
            info = _json_dict(answer.body)
            found = info.get("ContainerID") if info is not None else None
            if answer.status != 200 or not isinstance(found, str):
                return BadRequestError(502, "Docker sent an unexpected exec description")
            container = found
        if container is None:
            return BadRequestError(403, "refused by the sandcage proxy: nothing to check")
        answer = await self._upstream_get(f"/containers/{container}/json")
        if answer.status == 404:
            return BadRequestError(404, f"No such container: {container}")
        info = _json_dict(answer.body)
        if answer.status != 200 or info is None:
            return BadRequestError(502, "Docker sent an unexpected container description")
        config = info.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict) or labels.get(SANDBOX_LABEL[0]) != SANDBOX_LABEL[1]:
            return BadRequestError(403, "refused by the sandcage proxy: not a sandbox container")
        full_id = info.get("Id")
        if not isinstance(full_id, str) or not re.fullmatch(r"[0-9a-f]{64}", full_id):
            return BadRequestError(502, "Docker sent an unexpected container id")
        if decision.container is None:
            return target  # an exec route: exec ids are unique and are not rewritten
        raw_path, mark, query = target.partition("?")
        prefix = raw_path[: len(raw_path) - len(decision.path)]
        rewritten = decision.path.replace(
            f"/containers/{decision.container}", f"/containers/{full_id}", 1
        )
        return prefix + rewritten + mark + query

    async def _upstream_get(self, path: str) -> UpstreamAnswer:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket, limit=HEAD_LIMIT), self._timeout
        )
        try:
            head = f"GET {API_PREFIX}{path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n"
            up_writer.write(head.encode())
            await asyncio.wait_for(up_writer.drain(), self._timeout)
            return await asyncio.wait_for(_read_answer(up_reader), self._timeout)
        finally:
            up_writer.close()
            with contextlib.suppress(ConnectionError):
                await up_writer.wait_closed()

    # --- forwarding ------------------------------------------------------------------

    async def _forward(
        self,
        request: Request,
        target: str,
        decision: Decision,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upgrade = request.method == "POST" and _EXEC_START.fullmatch(decision.path) is not None
        lines = [f"{request.method} {target} HTTP/1.1", "Host: docker"]
        lines += [f"{key}: {value}" for key, value in request.headers if key in _KEEP_HEADERS]
        if request.body or request.method in {"POST", "PUT"}:
            lines.append(f"Content-Length: {len(request.body)}")
        lines += ["Connection: Upgrade", "Upgrade: tcp"] if upgrade else ["Connection: close"]
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket, limit=HEAD_LIMIT), self._timeout
        )
        try:
            up_writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + request.body)
            await asyncio.wait_for(up_writer.drain(), self._timeout)
            if upgrade:
                await self._pipe_until_either_side_ends(up_reader, reader, writer)
            else:
                await self._pipe(up_reader, writer)
        finally:
            up_writer.close()
            with contextlib.suppress(ConnectionError):
                await up_writer.wait_closed()

    async def _pipe(self, source: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await asyncio.wait_for(source.read(65536), self._idle):
            writer.write(chunk)
            await asyncio.wait_for(writer.drain(), self._idle)

    async def _pipe_until_either_side_ends(
        self,
        upstream: asyncio.StreamReader,
        client: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Docker's output goes to the client; any byte (or EOF) from the client ends the
        stream, since an exec has no stdin and nothing may ride along."""
        piping = asyncio.create_task(self._pipe(upstream, writer))
        watching = asyncio.create_task(client.read(1))
        try:
            await asyncio.wait({piping, watching}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (piping, watching):
                task.cancel()
            results = await asyncio.gather(piping, watching, return_exceptions=True)
        for result in results:
            if isinstance(result, OSError | TimeoutError):
                raise result


async def _read_answer(reader: asyncio.StreamReader) -> UpstreamAnswer:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head[:-4].decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split(" ")[1])
    except (IndexError, ValueError):
        raise OSError("Docker sent a malformed answer") from None
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in lines[1:])
    }
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = bytearray()
        while True:
            try:
                size = int((await reader.readuntil(b"\r\n"))[:-2].split(b";")[0], 16)
            except ValueError:
                raise OSError("Docker sent a malformed chunk") from None
            if size == 0 or len(body) + size > UPSTREAM_LIMIT:
                break
            body.extend((await reader.readexactly(size + 2))[:-2])
        return UpstreamAnswer(status, bytes(body))
    length = headers.get("content-length", "")
    if length.isdigit() and int(length) <= UPSTREAM_LIMIT:
        return UpstreamAnswer(status, await reader.readexactly(int(length)))
    return UpstreamAnswer(status, await reader.read(UPSTREAM_LIMIT))


def _json_dict(data: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _path_only(target: str) -> str:
    return target.partition("?")[0][:200]


async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    body = json.dumps({"message": message}).encode()
    head = (
        f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    writer.write(head.encode() + body)
    with contextlib.suppress(ConnectionError):
        await writer.drain()


# --- the process ---------------------------------------------------------------------


async def serve_forever(proxy: DockerProxy, host: str, port: int) -> None:
    server = await proxy.start(host, port)
    log.info("sandcage docker proxy listening on %s:%s", host, port)
    async with server:
        await server.serve_forever()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = ProxyConfig.from_env()
        port = int(os.environ.get("PROXY_PORT", "2375"))
    except (ProxyConfigError, ValueError) as problem:
        print(f"docker proxy: {problem}", file=sys.stderr)
        return 2
    host = os.environ.get("PROXY_HOST", "0.0.0.0")  # noqa: S104 - an internal-only network
    docker_socket = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
    asyncio.run(serve_forever(DockerProxy(config, docker_socket), host, port))
    return 0
