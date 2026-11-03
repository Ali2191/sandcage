"""The Docker container a run's commands execute in, and the reaper for orphans.

One container per run, started idle (``sleep infinity``) from :func:`hardened_config`,
with the run's workspace bind-mounted at /workspace. Each command is one exec:

- **The host owns the deadline.** It is set *before* ``exec_start``; a reader thread
  drains the output; the command timed out exactly when its output was still open at the
  deadline. Then every process in the container is killed. The in-container ``timeout``
  (deadline plus grace) is only a backstop. Exit code 124 or 137 alone never means
  "timed out", and no verdict compares two clocks.
- **The exit code is read only once Docker reports the exec finished**: ``exec_inspect``
  is polled for a bounded time, and the code is None if Docker never records one.
- **Output** is capped (the reader keeps draining past the cap so the command never
  blocks on a full pipe).
- **Kill-all after every command** (``kill -9 -1`` as the sandbox user spares only PID
  1), so nothing keeps running in the background between steps.

The reaper removes sandboxes whose owner on this host is gone: a dead pid, or our own
pid with another instance id (a restarted API container is PID 1 again with the same
host name). It deletes their temp workspace only when :func:`run_temp_dir` accepts it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sandcage.sandbox.docker_api import (
    DockerAPI,
    DockerError,
    DockerNotFoundError,
    ExecStream,
)
from sandcage.sandbox.hardening import (
    IMAGE,
    INSTANCE_LABEL,
    LABEL,
    LABEL_VALUE,
    OWNER_LABEL,
    SANDBOX_USER,
    WORKSPACE_MOUNT,
    OwnerIdentity,
    current_identity,
    hardened_config,
)
from sandcage.sandbox.workspace import run_temp_dir

COMMAND_TIMEOUT: Final = 30.0
OUTPUT_LIMIT: Final = 20_000
HOST_GRACE: Final = 5.0
EXIT_CODE_WAIT: Final = 2.0
EXIT_CODE_POLL: Final = 0.02
HELPER_TIMEOUT: Final = 5.0
KILL_ALL: Final = ("sh", "-c", "kill -9 -1")
OPEN_PERMISSIONS: Final = ("sh", "-c", f"chmod -R a+rwX {WORKSPACE_MOUNT} 2>/dev/null; true")
# $0 is the agent's command and $1 the backstop, so the command text is never re-quoted.
_WRAPPER: Final = 'umask 000 && exec timeout -s KILL "$1" sh -c "$0"'


class SandboxImageMissingError(DockerError):
    pass


class SandboxClosedError(Exception):
    """A command was sent to a sandbox that is not running."""


@dataclass(frozen=True)
class Limits:
    timeout: float = COMMAND_TIMEOUT
    output_limit: int = OUTPUT_LIMIT
    host_grace: float = HOST_GRACE
    exit_code_wait: float = EXIT_CODE_WAIT
    exit_code_poll: float = EXIT_CODE_POLL


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    output: str
    truncated: bool
    timed_out: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "output": self.output,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
        }


class OutputBuffer:
    """Keeps the first ``limit`` bytes and counts everything, so reading never stops."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self._total = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            self._total += len(chunk)
            room = self._limit - len(self._data)
            if room > 0:
                self._data.extend(chunk[:room])

    @property
    def truncated(self) -> bool:
        with self._lock:
            return self._total > self._limit

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def text(self) -> str:
        with self._lock:
            return bytes(self._data).decode("utf-8", errors="replace")


def ensure_image(api: DockerAPI, image: str = IMAGE) -> None:
    """The sandbox image must already exist: the Docker proxy refuses builds, so it is
    built on the host (``make sandbox-image``)."""
    if not api.image_exists(image):
        raise SandboxImageMissingError(
            f"the sandbox image {image} is missing; build it on the host with 'make sandbox-image'"
        )


class DockerSandbox:
    def __init__(
        self,
        api: DockerAPI,
        workspace: Path,
        *,
        runtime: str | None,
        identity: OwnerIdentity | None = None,
        image: str = IMAGE,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self._workspace = workspace
        self._runtime = runtime
        self._identity = identity or current_identity()
        self._image = image
        self._limits = limits or Limits()
        self._clock = clock
        self._sleep = sleep
        self._id: str | None = None
        self._closed = False

    @property
    def container_id(self) -> str | None:
        return self._id

    @property
    def limits(self) -> Limits:
        return self._limits

    def start(self) -> None:
        if self._id is not None or self._closed:
            raise SandboxClosedError("this sandbox was already started")
        ensure_image(self._api, self._image)
        config = hardened_config(
            command=["sleep", "infinity"],
            identity=self._identity,
            workspace=self._workspace,
            runtime=self._runtime,
            image=self._image,
        )
        container_id = self._api.create_container(config)
        self._id = container_id
        try:
            self._api.start_container(container_id)
        except BaseException:
            self.close()
            raise

    # --- commands --------------------------------------------------------------------

    def run(self, command: str) -> CommandResult:
        container_id = self._running_id()
        limits = self._limits
        backstop = f"{limits.timeout + limits.host_grace:g}"
        exec_id = self._api.exec_create(
            container_id, ["sh", "-c", _WRAPPER, command, backstop], user=SANDBOX_USER
        )
        deadline = self._clock() + limits.timeout  # set before the command starts
        stream = self._api.exec_start(exec_id, timeout=self._stream_timeout())
        buffer = OutputBuffer(limits.output_limit)
        ended = threading.Event()
        failures: list[DockerError | OSError] = []
        reader = threading.Thread(
            target=_drain, args=(stream, buffer, ended, failures), daemon=True
        )
        reader.start()
        timed_out = not ended.wait(max(0.0, deadline - self._clock()))
        try:
            self._kill_all(container_id)
        finally:
            if not ended.wait(limits.host_grace):
                stream.close()
                ended.wait(limits.host_grace)
            reader.join(limits.host_grace)
        if failures:
            raise failures[0]
        return CommandResult(self._exit_code(exec_id), buffer.text(), buffer.truncated, timed_out)

    def _running_id(self) -> str:
        if self._id is None or self._closed:
            raise SandboxClosedError("the sandbox is not running")
        return self._id

    def _stream_timeout(self) -> float:
        limits = self._limits
        return limits.timeout + 2 * limits.host_grace + HELPER_TIMEOUT

    def _exit_code(self, exec_id: str) -> int | None:
        """Wait (bounded) until Docker says the exec finished; only then trust ExitCode."""
        give_up = self._clock() + self._limits.exit_code_wait
        while True:
            state = self._api.exec_inspect(exec_id)
            code = state.get("ExitCode")
            if state.get("Running") is False and isinstance(code, int):
                return code
            if self._clock() >= give_up:
                return None
            self._sleep(self._limits.exit_code_poll)

    def _helper(self, container_id: str, command: tuple[str, ...]) -> None:
        """Run a short fixed helper (kill-all, permissions) and drain it, bounded."""
        exec_id = self._api.exec_create(container_id, list(command), user=SANDBOX_USER)
        stream = self._api.exec_start(exec_id, timeout=HELPER_TIMEOUT)
        ended = threading.Event()
        failures: list[DockerError | OSError] = []
        thread = threading.Thread(
            target=_drain, args=(stream, OutputBuffer(0), ended, failures), daemon=True
        )
        thread.start()
        if not ended.wait(HELPER_TIMEOUT):
            stream.close()
        thread.join(HELPER_TIMEOUT)

    def _kill_all(self, container_id: str) -> None:
        self._helper(container_id, KILL_ALL)

    # --- lifetime --------------------------------------------------------------------

    def close(self) -> None:
        """Remove the container (a missing one is fine). Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        container_id = self._id
        if container_id is None:
            return
        # Files the agent made private would block deleting the workspace on the host;
        # the container may already be dead, in which case there is nothing to fix.
        with contextlib.suppress(DockerError):
            self._helper(container_id, OPEN_PERMISSIONS)
        with contextlib.suppress(DockerNotFoundError):
            self._api.remove_container(container_id)


def _drain(
    stream: ExecStream,
    buffer: OutputBuffer,
    ended: threading.Event,
    failures: list[DockerError | OSError],
) -> None:
    """Reader thread: read everything (past the cap), stamp the end, record a failure."""
    try:
        for chunk in stream:
            buffer.append(chunk)
    except (DockerError, OSError) as problem:
        failures.append(problem)
    finally:
        ended.set()


# --- orphans -------------------------------------------------------------------------


@dataclass(frozen=True)
class Reaped:
    containers: tuple[str, ...]
    workspaces: tuple[str, ...]
    problems: tuple[str, ...]


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is just not ours to signal
    return True


def reap_orphaned_sandboxes(
    api: DockerAPI,
    *,
    identity: OwnerIdentity | None = None,
    temp_root: Path | None = None,
    alive: Callable[[int], bool] = pid_alive,
) -> Reaped:
    """Remove this host's sandboxes whose owning process is gone, with their workspaces.

    Containers of live processes, of other hosts, or without a readable owner label are
    never touched. Run at API start, before any run.
    """
    me = identity or current_identity()
    containers: list[str] = []
    workspaces: list[str] = []
    problems: list[str] = []
    for item in api.list_containers({LABEL: LABEL_VALUE}):
        container_id = item.get("Id")
        labels = item.get("Labels")
        if not isinstance(container_id, str) or not isinstance(labels, dict):
            continue
        if not _is_orphan(labels, me, alive):
            continue
        try:
            api.remove_container(container_id)
        except DockerNotFoundError:
            pass  # removed meanwhile: still an orphan handled
        except DockerError as problem:
            problems.append(f"{container_id[:12]}: {problem}")
            continue
        containers.append(container_id)
        workspace = _workspace_of(item)
        run_dir = run_temp_dir(workspace, temp_root) if workspace else None
        if run_dir is None:
            continue
        try:
            shutil.rmtree(run_dir)
        except OSError as problem:
            problems.append(f"{run_dir.name}: {problem.strerror or 'cannot delete'}")
            continue
        workspaces.append(str(run_dir))
    return Reaped(tuple(containers), tuple(workspaces), tuple(problems))


def _is_orphan(
    labels: Mapping[str, object], me: OwnerIdentity, alive: Callable[[int], bool]
) -> bool:
    owner = labels.get(OWNER_LABEL)
    instance = labels.get(INSTANCE_LABEL)
    if not isinstance(owner, str) or not isinstance(instance, str):
        return False
    host, _, pid_text = owner.rpartition(":")
    if host != me.host or not pid_text.isdigit():
        return False
    pid = int(pid_text)
    if pid == me.pid:
        return instance != me.instance
    return not alive(pid)


def _workspace_of(item: Mapping[str, object]) -> str | None:
    mounts = item.get("Mounts")
    if not isinstance(mounts, list):
        return None
    for mount in mounts:
        if isinstance(mount, dict) and mount.get("Destination") == WORKSPACE_MOUNT:
            source = mount.get("Source")
            return source if isinstance(source, str) else None
    return None
