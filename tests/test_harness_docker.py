"""Every scenario through the whole harness on real Docker (scripted models, real gate,
real hardened sandbox, the verifier's test run inside the container)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandcage.agent.scenarios import FIXED_LINE, ScenarioId
from sandcage.contract.approval import ApprovalKind
from sandcage.contract.events import DoneEvent, RunStatus, StepEvent, VerifyEvent
from sandcage.report.report import OUTCOME_DELIVERED, OUTCOME_WITHHELD
from sandcage.sandbox.container import DockerSandbox, Limits
from sandcage.sandbox.docker_api import DockerError, EngineClient
from sandcage.sandbox.run import RunnerFactory
from tests.harness_helpers import ScriptedHuman, refuse, run_scenario

pytestmark = pytest.mark.docker


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


def _done(events: list[object]) -> DoneEvent:
    done = events[-1]
    assert isinstance(done, DoneEvent)
    return done


@pytest.mark.parametrize("scenario", [s for s in ScenarioId if s is not ScenarioId.SNEAKY_URLLIB])
def test_scenario_on_real_docker(
    tmp_path: Path, docker: RunnerFactory, scenario: ScenarioId
) -> None:
    run = run_scenario(tmp_path, scenario, runner_factory=docker)
    result = run.result
    assert result.status is RunStatus.FINISHED, result.summary
    assert FIXED_LINE in run.workspace_files["invoice_calc/invoice.py"]
    verify = [e for e in result.events if isinstance(e, VerifyEvent)]
    assert verify[-1].passed
    assert verify[-1].tests is not None
    assert verify[-1].tests.passed  # the verifier's own run, inside the container
    done = _done(list(result.events))
    assert (done.outcome, done.delivered) == (OUTCOME_DELIVERED, True)
    if scenario is ScenarioId.DEMO:
        blocked = [e.rule_id for e in result.events if isinstance(e, StepEvent) and e.blocked]
        assert blocked == ["net.deny_tools", "path.deny_secrets"]
        assert done.breaker_trips == 1


def test_sneaky_patch_withheld_on_real_docker(tmp_path: Path, docker: RunnerFactory) -> None:
    run = run_scenario(
        tmp_path,
        "sneaky_urllib",
        runner_factory=docker,
        human=ScriptedHuman(answers={ApprovalKind.REDTEAM: refuse}),
    )
    done = _done(list(run.result.events))
    assert (done.outcome, done.delivered, done.delivery_blocked) == (
        OUTCOME_WITHHELD,
        False,
        True,
    )
    assert run.result.outputs is not None
    assert run.result.outputs.blocked
