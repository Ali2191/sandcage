"""A scripted fake of the Docker API, for every sandbox test that must run without Docker.

Faults are injected, never slept around: an exec stream can stay open until a kill-all
exec arrives, ``exec_start`` can stall after the command has started, and
``exec_inspect`` can report a stale "still running" a given number of times.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from sandcage.sandbox.docker_api import (
    DockerError,
    DockerNotFoundError,
    DockerUnavailableError,
)

KILL_ALL = "kill -9 -1"


@dataclass
class Script:
    """How one exec behaves."""

    output: list[bytes] = field(default_factory=list)
    exit_code: int | None = 0
    hold_open: bool = False  # the stream stays open until a kill-all (or close)
    runs_for: float = 0.0  # the command ends by itself this long after it started
    stale_inspects: int = 0  # exec_inspect says "running" this many times after the end
    killed_exit_code: int = 137


@dataclass
class FakeExec:
    exec_id: str
    container_id: str
    command: list[str]
    user: str
    script: Script
    released: threading.Event = field(default_factory=threading.Event)
    finished: bool = False
    killed: bool = False
    inspects: int = 0
    started_at: float = 0.0

    @property
    def exit_code(self) -> int | None:
        return self.script.killed_exit_code if self.killed else self.script.exit_code


class FakeStream:
    def __init__(self, record: FakeExec, on_end: Callable[[], None]) -> None:
        self._record = record
        self._on_end = on_end
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._record.script.output
            ends_at = self._record.started_at + self._record.script.runs_for
            while (left := ends_at - time.monotonic()) > 0:
                if self._record.released.wait(left):
                    self._record.killed = True
                    return
            if self._record.script.hold_open:
                while not self._record.released.wait(0.01):
                    if self.closed:
                        return
        finally:
            self._on_end()

    def close(self) -> None:
        self.closed = True
        self._record.released.set()


@dataclass
class FakeContainer:
    container_id: str
    config: Mapping[str, object]
    running: bool = False
    logs: bytes = b""


class FakeDocker:
    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}
        self.created: list[Mapping[str, object]] = []
        self.removed: list[str] = []
        self.execs: dict[str, FakeExec] = {}
        self.calls: list[str] = []
        self.runtimes: dict[str, object] = {"runc": {}}
        self.images: set[str] = {"sandcage-sandbox:latest"}
        self.listing: list[Mapping[str, object]] = []
        self.inspections: dict[str, Mapping[str, object]] = {}
        self.unavailable = False
        self.failures: dict[str, DockerError] = {}
        self.exec_start_stall = 0.0  # seconds exec_start waits after the command started
        self.scripts: Callable[[list[str]], Script] = lambda _command: Script()
        self.probe_logs = b""
        self._ids = itertools.count(1)

    # --- helpers ---------------------------------------------------------------------

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.unavailable:
            raise DockerUnavailableError("cannot reach Docker (FileNotFoundError)")
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def _container(self, container_id: str) -> FakeContainer:
        if container_id not in self.containers:
            raise DockerNotFoundError(f"No such container: {container_id}")
        return self.containers[container_id]

    def commands(self) -> list[list[str]]:
        return [record.command for record in self.execs.values()]

    # --- the protocol ----------------------------------------------------------------

    def ping(self) -> None:
        self._enter("ping")

    def info(self) -> Mapping[str, object]:
        self._enter("info")
        return {"Runtimes": dict(self.runtimes)}

    def image_exists(self, image: str) -> bool:
        self._enter("image_exists")
        return image in self.images

    def create_container(self, config: Mapping[str, object]) -> str:
        self._enter("create_container")
        container_id = f"c{next(self._ids):04d}"
        self.created.append(config)
        self.containers[container_id] = FakeContainer(container_id, config)
        return container_id

    def start_container(self, container_id: str) -> None:
        self._enter("start_container")
        container = self._container(container_id)
        container.running = True
        container.logs = self.probe_logs

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        self._enter("wait_container")
        self._container(container_id).running = False
        return 0

    def container_logs(self, container_id: str) -> bytes:
        self._enter("container_logs")
        return self._container(container_id).logs

    def inspect_container(self, container_id: str) -> Mapping[str, object]:
        self._enter("inspect_container")
        if container_id in self.inspections:
            return self.inspections[container_id]
        container = self._container(container_id)
        return {"Id": container_id, "Config": container.config}

    def remove_container(self, container_id: str) -> None:
        self._enter("remove_container")
        self.removed.append(container_id)
        if container_id not in self.containers and all(
            item.get("Id") != container_id for item in self.listing
        ):
            raise DockerNotFoundError(f"No such container: {container_id}")
        self.containers.pop(container_id, None)
        self.listing = [item for item in self.listing if item.get("Id") != container_id]

    def list_containers(self, labels: Mapping[str, str]) -> list[Mapping[str, object]]:
        self._enter("list_containers")
        wanted = set(labels.items())
        found: list[Mapping[str, object]] = []
        for item in self.listing:
            item_labels = item.get("Labels")
            if isinstance(item_labels, dict) and wanted <= set(item_labels.items()):
                found.append(item)
        return found

    def exec_create(self, container_id: str, command: Sequence[str], *, user: str) -> str:
        self._enter("exec_create")
        container = self._container(container_id)
        if not container.running:
            raise DockerError("Docker answered 409: container is not running")
        exec_id = f"e{next(self._ids):04d}"
        command = list(command)
        script = Script() if KILL_ALL in " ".join(command) else self.scripts(command)
        self.execs[exec_id] = FakeExec(exec_id, container_id, command, user, script)
        return exec_id

    def exec_start(self, exec_id: str, *, timeout: float) -> FakeStream:
        self._enter("exec_start")
        record = self.execs[exec_id]
        record.started_at = time.monotonic()
        if KILL_ALL in " ".join(record.command):
            for other in self.execs.values():
                if other.container_id == record.container_id and not other.finished:
                    other.killed = other.script.hold_open
                    other.released.set()
        if self.exec_start_stall:
            time.sleep(self.exec_start_stall)  # the injected client stall

        def ended() -> None:
            record.finished = True

        return FakeStream(record, ended)

    def exec_inspect(self, exec_id: str) -> Mapping[str, object]:
        self._enter("exec_inspect")
        record = self.execs[exec_id]
        record.inspects += 1
        if not record.finished or record.inspects <= record.script.stale_inspects:
            return {"Running": True, "ExitCode": None}
        if record.exit_code is None:
            return {"Running": True, "ExitCode": None}
        return {"Running": False, "ExitCode": record.exit_code}
