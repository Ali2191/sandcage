"""Checkpoints on real Docker: files changed, deleted and created by the sandbox user
(uid 65534, through the bind mount) are restored exactly by a rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandcage.harness.checkpoints import Checkpoints
from sandcage.sandbox import safefs
from sandcage.sandbox.container import DockerSandbox, Limits
from sandcage.sandbox.docker_api import DockerError, EngineClient
from sandcage.sandbox.run import SandboxRun, StepResult
from tests.policy_helpers import engine
from tests.sandbox_helpers import DEMO_REPO

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


def _run(run: SandboxRun, action: dict[str, object]) -> StepResult:
    result = run.execute(action)
    if not result.executed and result.decision.decision.value == "ask":
        result = run.execute_approved(action)
    assert result.executed, result.decision.reason
    return result


def _files(root: Path) -> dict[str, bytes]:
    return {p: safefs.read_bytes(root, p, limit=1_000_000)[0] for p in safefs.walk(root)}


def test_rollback_restores_container_made_changes_exactly(
    api: EngineClient, tmp_path: Path
) -> None:
    checkpoints = Checkpoints(DEMO_REPO)
    run = SandboxRun(
        DEMO_REPO,
        engine=engine(),
        runner_factory=lambda ws: DockerSandbox(api, ws, runtime=None, limits=Limits(timeout=30.0)),
        on_write=checkpoints.on_write,
        tools={"rollback": checkpoints.rollback},
        temp_root=tmp_path,
    )
    with run:
        workspace = run.workspace.path
        checkpoints.attach(workspace, run.workspace.left_out)
        before = _files(workspace)
        write = _run(
            run,
            {"tool": "write_file", "args": {"path": "notes.txt", "content": "checkpoint me\n"}},
        )
        assert isinstance(write.result, dict)
        assert write.result["checkpoint"] == 1
        at_one = _files(workspace)
        command = (
            "python -c \"import os; open('invoice_calc/invoice.py','a').write('# changed\\n'); "
            "os.remove('requirements.txt'); open('created.txt','w').write('new\\n')\""
        )
        changed = _run(run, {"tool": "run_command", "args": {"command": command}})
        assert isinstance(changed.result, dict)
        assert changed.result["exit_code"] == 0, changed.result
        after = _files(workspace)
        assert after["invoice_calc/invoice.py"] != at_one["invoice_calc/invoice.py"]
        assert "requirements.txt" not in after
        assert "created.txt" in after
        rollback = _run(run, {"tool": "rollback", "args": {"checkpoint": 1}})
        assert isinstance(rollback.result, dict)
        restored = rollback.result["restored"]
        assert isinstance(restored, list)
        assert sorted(str(path) for path in restored) == [
            "created.txt",
            "invoice_calc/invoice.py",
            "requirements.txt",
        ]
        assert _files(workspace) == at_one
        tests = _run(run, {"tool": "run_command", "args": {"command": "pytest -q"}})
        assert isinstance(tests.result, dict)
        assert "1 failed, 4 passed" in str(tests.result["output"])
        assert _run(run, {"tool": "rollback", "args": {"checkpoint": 0}}).executed
        assert {k: v for k, v in _files(workspace).items() if "__pycache__" not in k} == {
            k: v for k, v in before.items() if "__pycache__" not in k
        }
