"""The proxy in front of the real Docker socket (``make test-docker``)."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandcage.dockerproxy.policy import ProxyConfig
from sandcage.sandbox.container import DockerSandbox, Limits, reap_orphaned_sandboxes
from sandcage.sandbox.docker_api import (
    DockerError,
    DockerRefusedError,
    EngineClient,
)
from sandcage.sandbox.hardening import current_identity, hardened_config
from sandcage.sandbox.runtime import RuntimeSelector
from sandcage.sandbox.workspace import Workspace
from tests.sandbox_helpers import DEMO_REPO, is_secret
from tests.test_dockerproxy_server import Running, start_proxy, stop_proxy, wait_until

pytestmark = pytest.mark.docker

DOCKER_SOCKET = "/var/run/docker.sock"


@pytest.fixture(scope="module")
def direct() -> EngineClient:
    client = EngineClient.from_env()
    try:
        client.ping()
    except DockerError as problem:
        pytest.fail(f"make test-docker needs a running Docker daemon: {problem}")
    return client


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return Path(os.path.realpath(tmp_path))


@pytest.fixture
def through(work: Path, direct: EngineClient) -> Iterator[tuple[EngineClient, Running]]:
    host = os.environ.get("DOCKER_HOST", f"unix://{DOCKER_SOCKET}")
    socket_path = host.removeprefix("unix://")
    running = start_proxy(socket_path)
    running.proxy.config = ProxyConfig("sandcage-sandbox:latest", str(work))
    try:
        yield EngineClient(f"tcp://127.0.0.1:{running.port}", timeout=30), running
    finally:
        stop_proxy(running)


def test_a_real_sandbox_runs_through_the_proxy(
    through: tuple[EngineClient, Running], work: Path
) -> None:
    client, running = through
    with Workspace.create(DEMO_REPO, leave_out=is_secret(), temp_root=work) as ws:
        sandbox = DockerSandbox(client, ws.path, runtime=None, limits=Limits(timeout=20.0))
        sandbox.start()
        try:
            result = sandbox.run("python -m pytest -q -p no:cacheprovider")
            assert "1 failed, 4 passed" in result.output
            assert result.exit_code == 1
        finally:
            sandbox.close()
    assert wait_until(lambda: running.tasks() == 0)  # exec streams left nothing open


def test_ping_list_and_the_runtime_probe_work_through_the_proxy(
    through: tuple[EngineClient, Running],
) -> None:
    client, _ = through
    client.ping()
    assert isinstance(client.list_containers({"sandcage": "sandbox"}), list)
    status = RuntimeSelector(client, "runsc").status()
    assert status.active in {"runc", "runsc"}
    assert reap_orphaned_sandboxes(client, alive=lambda _pid: True).problems == ()


def refused_creates(work: Path) -> dict[str, dict[str, object]]:
    base = hardened_config(
        command=["sleep", "infinity"],
        identity=current_identity(),
        workspace=work / "sandcage-x" / "workspace",
        runtime=None,
    )

    def host(**changes: object) -> dict[str, object]:
        body = copy.deepcopy(base)
        host_config = body["HostConfig"]
        assert isinstance(host_config, dict)
        host_config.update(changes)
        return body

    other = copy.deepcopy(base)
    other["Image"] = "alpine:3.20"
    return {
        "privileged": host(Privileged=True),
        "host bind": host(Binds=["/etc:/workspace"]),
        "socket mount": host(Binds=[f"{DOCKER_SOCKET}:/workspace"]),
        "other image": other,
    }


def test_unsafe_creates_are_refused_and_nothing_is_created(
    through: tuple[EngineClient, Running], direct: EngineClient, work: Path
) -> None:
    client, _ = through
    before = {c.get("Id") for c in direct.list_containers({"sandcage": "sandbox"})}
    for name, body in refused_creates(work).items():
        with pytest.raises(DockerRefusedError, match="refused by the sandcage proxy"):
            client.create_container(body)
        after = {c.get("Id") for c in direct.list_containers({"sandcage": "sandbox"})}
        assert after == before, name


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/build"),
        ("POST", "/images/create?fromImage=alpine&tag=3.20"),
        ("POST", "/volumes/create"),
        ("GET", "/volumes"),
        ("POST", "/networks/create"),
    ],
)
def test_builds_pulls_and_volumes_are_refused(
    through: tuple[EngineClient, Running], method: str, path: str
) -> None:
    client, _ = through
    with pytest.raises(DockerRefusedError):
        client._call(method, path, body={})


def test_exec_into_a_container_that_is_not_a_sandbox_is_refused(
    through: tuple[EngineClient, Running], direct: EngineClient
) -> None:
    client, _ = through
    # The same image, but created directly and without the sandbox label: the proxy must
    # judge the label, not the image.
    plain = direct.create_container(
        {
            "Image": "sandcage-sandbox:latest",
            "Cmd": ["sleep", "60"],
            "Labels": {"purpose": "proxy-test"},
            "HostConfig": {"NetworkMode": "none"},
        }
    )
    try:
        direct.start_container(plain)
        with pytest.raises(DockerRefusedError, match="not a sandbox container"):
            client.exec_create(plain, ["id"], user="65534:65534")
        with pytest.raises(DockerRefusedError):
            client.remove_container(plain)
        info = direct.inspect_container(plain)
        state = info.get("State")
        assert isinstance(state, dict)
        assert state.get("Running") is True
    finally:
        direct.remove_container(plain)


def test_a_refused_request_body_is_reported(through: tuple[EngineClient, Running]) -> None:
    client, _ = through
    with pytest.raises(DockerRefusedError) as info:
        client._call("POST", "/containers/create", body=json.loads('{"Image": "alpine:3.20"}'))
    assert "sandbox" in str(info.value)
