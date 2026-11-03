"""Runtime selection: gVisor only when its kernel banner proves it; never claimed falsely."""

from __future__ import annotations

import pytest

from sandcage.sandbox.docker_api import DockerError
from sandcage.sandbox.hardening import OwnerIdentity, hardened_config
from sandcage.sandbox.runtime import (
    RuntimeConfigError,
    RuntimeSelector,
    RuntimeStatus,
    requested_runtime,
)
from tests.fake_docker import FakeDocker

IDENTITY = OwnerIdentity("host-a", 4242, "inst1")
BANNER = b"[    0.000000] Starting gVisor...\n[    0.123456] Waiting for children...\n"


def selector(docker: FakeDocker, requested: str = "runsc") -> RuntimeSelector:
    return RuntimeSelector(docker, requested, identity=IDENTITY)


def test_requested_runtime_comes_from_the_environment() -> None:
    assert requested_runtime({}) == "runc"
    assert requested_runtime({"SANDBOX_RUNTIME": " runsc "}) == "runsc"
    assert requested_runtime({"SANDBOX_RUNTIME": ""}) == "runc"
    for bad in ("gvisor", "kata", "RUNSC"):
        with pytest.raises(RuntimeConfigError):
            requested_runtime({"SANDBOX_RUNTIME": bad})
    with pytest.raises(RuntimeConfigError):
        RuntimeSelector(FakeDocker(), "kata")


def test_plain_runtime_needs_no_probe() -> None:
    docker = FakeDocker()
    status = selector(docker, "runc").status()
    assert status == RuntimeStatus("runc", "runc", "plain Docker (runc) was requested")
    assert not status.gvisor
    assert docker.calls == []


def test_gvisor_is_verified_by_its_kernel_banner() -> None:
    docker = FakeDocker()
    docker.runtimes["runsc"] = {"path": "/usr/local/bin/runsc"}
    docker.probe_logs = BANNER
    status = selector(docker).status()
    assert (status.active, status.gvisor, status.label) == ("runsc", True, "gVisor (runsc)")
    assert "kernel boot banner" in status.reason
    assert len(docker.created) == 1
    assert len(docker.removed) == 1  # the probe container is always removed


def test_the_probe_is_hardened_like_a_sandbox() -> None:
    docker = FakeDocker()
    docker.runtimes["runsc"] = {}
    docker.probe_logs = BANNER
    selector(docker).status()
    (config,) = docker.created
    assert config == hardened_config(
        command=["dmesg"], identity=IDENTITY, workspace=None, runtime="runsc"
    )
    host = config["HostConfig"]
    assert isinstance(host, dict)
    assert host["Runtime"] == "runsc"
    assert "Binds" not in host
    assert host["NetworkMode"] == "none"


def test_a_runtime_that_only_claims_the_name_falls_back() -> None:
    docker = FakeDocker()
    docker.runtimes["runsc"] = {"path": "runc"}  # registered, but really plain runc
    docker.probe_logs = b"dmesg: read kernel buffer failed: Operation not permitted\n"
    status = selector(docker).status()
    assert (status.active, status.gvisor) == ("runc", False)
    assert "did not show gVisor's kernel banner" in status.reason
    assert status.label == "runc (plain Docker)"


def test_not_registered_falls_back_without_a_probe() -> None:
    docker = FakeDocker()
    status = selector(docker).status()
    assert (status.active, status.reason) == (
        "runc",
        "gVisor (runsc) is not registered with Docker",
    )
    assert "create_container" not in docker.calls


def test_a_failing_probe_falls_back_and_cleans_up() -> None:
    docker = FakeDocker()
    docker.runtimes["runsc"] = {}
    docker.failures["start_container"] = DockerError("Docker answered 500: runsc: cannot boot")
    status = selector(docker).status()
    assert status.active == "runc"
    assert "the gVisor probe failed" in status.reason
    assert "cannot boot" in status.reason
    assert len(docker.removed) == 1


def test_docker_unreachable_is_unknown_and_probed_again() -> None:
    docker = FakeDocker()
    docker.runtimes["runsc"] = {}
    docker.probe_logs = BANNER
    docker.unavailable = True
    sel = selector(docker)
    first = sel.status()
    assert (first.active, first.gvisor, first.label) == (
        None,
        False,
        "unknown (Docker unreachable)",
    )
    docker.unavailable = False
    second = sel.status()
    assert second.gvisor
    docker.calls.clear()
    assert sel.status() is second  # cached after a real answer
    assert docker.calls == []


def test_status_serialises_for_health_and_reports() -> None:
    status = RuntimeStatus("runsc", "runc", "gVisor (runsc) is not registered with Docker")
    assert status.to_dict() == {
        "requested": "runsc",
        "active": "runc",
        "gvisor": False,
        "label": "runc (plain Docker)",
        "reason": "gVisor (runsc) is not registered with Docker",
    }
