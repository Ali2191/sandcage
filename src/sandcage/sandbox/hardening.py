"""The hardened container configuration every sandbox (and the runtime probe) uses, and
the labels that let the reaper recognise its own containers.

The Docker socket proxy checks this shape independently (it imports nothing from here);
a test proves the two agree.
"""

from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final

IMAGE: Final = "sandcage-sandbox:latest"
SANDBOX_USER: Final = "65534:65534"
LABEL: Final = "sandcage"
LABEL_VALUE: Final = "sandbox"
OWNER_LABEL: Final = "sandcage.owner"
INSTANCE_LABEL: Final = "sandcage.instance"
MEMORY_BYTES: Final = 512 * 1024 * 1024
PIDS_LIMIT: Final = 128
NANO_CPUS: Final = 1_000_000_000
TMPFS_OPTIONS: Final = "rw,noexec,nosuid,nodev,size=64m"
WORKSPACE_MOUNT: Final = "/workspace"
GVISOR_RUNTIME: Final = "runsc"
PLAIN_RUNTIME: Final = "runc"


@dataclass(frozen=True)
class OwnerIdentity:
    """Who started a sandbox: host name, process id and a random per-process instance id.

    Inside a container the API is PID 1 with a fixed host name every time it restarts, so
    "is the owner alive" can never be judged by the pid alone (see the reaper).
    """

    host: str
    pid: int
    instance: str

    @property
    def owner(self) -> str:
        return f"{self.host}:{self.pid}"

    def labels(self) -> dict[str, str]:
        return {LABEL: LABEL_VALUE, OWNER_LABEL: self.owner, INSTANCE_LABEL: self.instance}


@cache
def current_identity() -> OwnerIdentity:
    """This process's identity, made once (the instance id is random per process)."""
    return OwnerIdentity(socket.gethostname(), os.getpid(), secrets.token_hex(8))


def hardened_config(
    *,
    command: list[str],
    identity: OwnerIdentity,
    workspace: Path | None,
    runtime: str | None,
    image: str = IMAGE,
) -> dict[str, object]:
    """The create request for a locked-down container: no network, read-only root, an
    unprivileged user, every capability dropped, no new privileges, process, memory and
    CPU limits, a small noexec /tmp, and at most the run's workspace mounted read-write."""
    host_config: dict[str, object] = {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges"],
        "PidsLimit": PIDS_LIMIT,
        "Memory": MEMORY_BYTES,
        "MemorySwap": MEMORY_BYTES,
        "NanoCpus": NANO_CPUS,
        "Tmpfs": {"/tmp": TMPFS_OPTIONS},  # noqa: S108 - the container's own tmpfs
    }
    if workspace is not None:
        host_config["Binds"] = [f"{workspace}:{WORKSPACE_MOUNT}:rw"]
    if runtime is not None and runtime != PLAIN_RUNTIME:
        host_config["Runtime"] = runtime
    return {
        "Image": image,
        "Cmd": command,
        "User": SANDBOX_USER,
        "WorkingDir": WORKSPACE_MOUNT,
        "Env": [],
        "Labels": identity.labels(),
        "NetworkDisabled": True,
        "AttachStdin": False,
        "AttachStdout": False,
        "AttachStderr": False,
        "Tty": False,
        "OpenStdin": False,
        "HostConfig": host_config,
    }
