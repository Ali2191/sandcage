"""The scripted demo at the agent level through a real SandboxRun on real Docker."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandcage.agent.loop import Agent
from sandcage.agent.scenarios import BUGGY_LINE, ScenarioId, scripted_models
from sandcage.contract.events import RunStatus
from sandcage.plan.layer import PlanLayer
from sandcage.sandbox.container import DockerSandbox, Limits
from sandcage.sandbox.docker_api import DockerError, EngineClient
from sandcage.sandbox.run import SandboxRun
from tests.agent_helpers import DEMO_PLAN, IS_SECRET
from tests.policy_helpers import engine
from tests.sandbox_helpers import DEMO_REPO, DEMO_SECRET_VALUE

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def api() -> EngineClient:
    client = EngineClient.from_env()
    try:
        client.ping()
    except DockerError as problem:
        pytest.fail(f"make test-docker needs a running Docker daemon: {problem}")
    if not client.image_exists("sandcage-sandbox:latest"):
        pytest.fail("build the sandbox image first: make sandbox-image")
    return client


def test_the_scripted_demo_on_real_docker(api: EngineClient, tmp_path: Path) -> None:
    run = SandboxRun(
        DEMO_REPO,
        engine=engine(),
        runner_factory=lambda ws: DockerSandbox(api, ws, runtime=None, limits=Limits(timeout=30.0)),
        layers=(PlanLayer(DEMO_PLAN, engine().rulebook.context),),
        temp_root=tmp_path,
    )
    models = scripted_models(ScenarioId.DEMO)
    with run:
        run.session.mark_suspicious("README.md")
        run.session.mark_suspicious("invoice_calc/invoice.py")
        result = Agent(models.executor, run, "dockerdemo01", IS_SECRET).run_task("Fix the test.")
        fixed = run.read_text("invoice_calc/invoice.py")
    assert result.status is RunStatus.FINISHED
    blocked = [e for e in result.events if e.blocked]
    assert [e.rule_id for e in blocked] == ["net.deny_tools", "path.deny_secrets"]
    assert all(
        e.source == "README.md" and e.tags == ["prompt_injection_suspected"] for e in blocked
    )
    tests = [e for e in result.events if e.tool == "run_command" and e.executed]
    assert "1 failed, 4 passed" in tests[0].output_preview
    assert "5 passed" in tests[1].output_preview
    assert fixed is not None
    assert BUGGY_LINE not in fixed
    assert DEMO_SECRET_VALUE not in models.executor.seen
