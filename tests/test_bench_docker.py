"""The scripted Bench in the real sandbox (``make test-docker``): the attacks that need real
commands (``rm`` of a watched file, ``curl`` to the collector, pytest in the container) are
scored the same way as with the host runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandcage.bench.results import as_expected
from sandcage.bench.runner import run_suite
from sandcage.sandbox.container import DockerSandbox, Limits
from sandcage.sandbox.docker_api import DockerError, EngineClient
from sandcage.sandbox.run import RunnerFactory


@pytest.fixture(scope="module")
def docker() -> RunnerFactory:
    client = EngineClient.from_env()
    try:
        client.ping()
    except DockerError as problem:
        pytest.fail(f"make test-docker needs a running Docker daemon: {problem}")
    if not client.image_exists("sandcage-sandbox:latest"):
        pytest.fail("build the sandbox image first: make sandbox-image")
    return lambda ws: DockerSandbox(client, ws, runtime=None, limits=Limits(timeout=30.0))


@pytest.mark.docker
def test_the_whole_scripted_bench_comes_out_as_designed_on_real_docker(
    tmp_path: Path, docker: RunnerFactory
) -> None:
    results = run_suite(runner_factory=docker, temp_root=tmp_path)
    assert len(results) == 12
    for result in results:
        for found in (result.baseline, result.full):
            assert found is not None
            assert found.error is None, (result.scenario.id, found.error)
            assert as_expected(result.scenario, found), (result.scenario.id, found)
    rm = next(r for r in results if r.scenario.id == "failure_rm")
    assert rm.baseline is not None
    assert rm.baseline.attack_succeeded is True
    assert rm.full is not None
    assert rm.full.attack_succeeded is False
    assert list(tmp_path.glob("sandcage-bench-*")) == []
