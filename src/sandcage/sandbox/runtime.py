"""Choose the OCI runtime for sandboxes: gVisor (``runsc``) only when it demonstrably runs.

``SANDBOX_RUNTIME`` asks for ``runsc`` or ``runc`` (default ``runc``; anything else is a
configuration error). gVisor is trusted only after a tiny hardened probe container,
started under ``runsc``, prints gVisor's own kernel boot banner. If ``runsc`` is not
registered, fails to start, or only claims the name, sandboxes use plain ``runc`` and the
status says why, so nothing ever claims gVisor when it is not running. Docker being
unreachable leaves the runtime unknown and is probed again next time.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from sandcage.masking import mask
from sandcage.sandbox.docker_api import (
    DockerAPI,
    DockerError,
    DockerNotFoundError,
    DockerUnavailableError,
)
from sandcage.sandbox.hardening import (
    GVISOR_RUNTIME,
    IMAGE,
    PLAIN_RUNTIME,
    OwnerIdentity,
    current_identity,
    hardened_config,
)

GVISOR_BANNER: Final = "Starting gVisor"
PROBE_TIMEOUT: Final = 30.0
RUNTIME_SETTING: Final = "SANDBOX_RUNTIME"
_CHOICES: Final = (GVISOR_RUNTIME, PLAIN_RUNTIME)


class RuntimeConfigError(ValueError):
    pass


def requested_runtime(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get(RUNTIME_SETTING, "").strip() or PLAIN_RUNTIME
    if value not in _CHOICES:
        raise RuntimeConfigError(f"{RUNTIME_SETTING} must be one of {', '.join(_CHOICES)}")
    return value


@dataclass(frozen=True)
class RuntimeStatus:
    requested: str
    active: str | None  # None: unknown, Docker unreachable
    reason: str

    @property
    def gvisor(self) -> bool:
        return self.active == GVISOR_RUNTIME

    @property
    def label(self) -> str:
        if self.active is None:
            return "unknown (Docker unreachable)"
        return "gVisor (runsc)" if self.gvisor else "runc (plain Docker)"

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "active": self.active,
            "gvisor": self.gvisor,
            "label": self.label,
            "reason": self.reason,
        }


class RuntimeSelector:
    """Probes once and caches the answer (re-probes only after Docker was unreachable)."""

    def __init__(
        self,
        api: DockerAPI,
        requested: str,
        *,
        image: str = IMAGE,
        identity: OwnerIdentity | None = None,
        probe_timeout: float = PROBE_TIMEOUT,
    ) -> None:
        if requested not in _CHOICES:
            raise RuntimeConfigError(f"{RUNTIME_SETTING} must be one of {', '.join(_CHOICES)}")
        self._api = api
        self._requested = requested
        self._image = image
        self._identity = identity or current_identity()
        self._timeout = probe_timeout
        self._cached: RuntimeStatus | None = None
        self._lock = threading.Lock()

    def status(self) -> RuntimeStatus:
        with self._lock:
            if self._cached is None:
                found = self._probe()
                if found.active is not None:
                    self._cached = found
                return found
            return self._cached

    def _probe(self) -> RuntimeStatus:
        if self._requested == PLAIN_RUNTIME:
            return RuntimeStatus(PLAIN_RUNTIME, PLAIN_RUNTIME, "plain Docker (runc) was requested")
        try:
            runtimes = self._api.info().get("Runtimes")
            if not isinstance(runtimes, dict) or GVISOR_RUNTIME not in runtimes:
                return self._fallback("gVisor (runsc) is not registered with Docker")
            output = self._run_probe()
        except DockerUnavailableError:
            return RuntimeStatus(GVISOR_RUNTIME, None, "Docker is unreachable")
        except DockerError as problem:
            return self._fallback(f"the gVisor probe failed: {mask(str(problem))}")
        if GVISOR_BANNER in output:
            return RuntimeStatus(
                GVISOR_RUNTIME, GVISOR_RUNTIME, "gVisor verified by its kernel boot banner"
            )
        return self._fallback(
            "the probe ran under runsc but did not show gVisor's kernel banner, "
            "so gVisor is not trusted"
        )

    def _run_probe(self) -> str:
        config = hardened_config(
            command=["dmesg"],
            identity=self._identity,
            workspace=None,
            runtime=GVISOR_RUNTIME,
            image=self._image,
        )
        container_id = self._api.create_container(config)
        try:
            self._api.start_container(container_id)
            self._api.wait_container(container_id, timeout=self._timeout)
            return self._api.container_logs(container_id).decode("utf-8", errors="replace")
        finally:
            with contextlib.suppress(DockerNotFoundError):  # already gone is fine
                self._api.remove_container(container_id)

    def _fallback(self, reason: str) -> RuntimeStatus:
        return RuntimeStatus(GVISOR_RUNTIME, PLAIN_RUNTIME, reason)
