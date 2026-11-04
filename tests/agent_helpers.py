"""Helpers for agent tests: a local runner that really runs the demo's tests in the
workspace copy (no Docker), a tiny checkpointer for the rollback scenario, and a builder
for a real SandboxRun with the plan locked."""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sandcage.contract.common import Manifest
from sandcage.contract.wording import secret_check
from sandcage.masking import JsonValue
from sandcage.plan.layer import PlanLayer
from sandcage.policy.actions import Action
from sandcage.sandbox.container import CommandResult
from sandcage.sandbox.run import SandboxRun
from sandcage.sandbox.workspace import Workspace
from tests.policy_helpers import engine
from tests.sandbox_helpers import DEMO_REPO

DEMO_PLAN = Manifest(
    read_paths=["README.md", "invoice_calc/**", "tests/**", "pytest.ini"],
    write_paths=["invoice_calc/**"],
    commands=["pytest", "python", "ls", "cat"],
)
IS_SECRET = secret_check(engine().rulebook.context)


@dataclass
class LocalRunner:
    """Runs the (already gated) commands of the tests on the host, in the workspace copy:
    ``pytest`` through this interpreter, ``ls`` and ``cat`` as themselves."""

    workspace: Path
    commands: list[str] = field(default_factory=list)

    def start(self) -> None:
        return None

    def run(self, command: str) -> CommandResult:
        self.commands.append(command)
        argv = shlex.split(command)
        if argv and argv[0] == "pytest":
            argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *argv[1:]]
        elif argv[:3] == ["python", "-m", "pytest"]:
            argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *argv[3:]]
        elif not argv or argv[0] not in {"ls", "cat"}:
            return CommandResult(
                127, f"{argv[0] if argv else ''}: not available here\n", False, False
            )
        done = subprocess.run(  # noqa: S603 - fixed programs, args from gated test scripts
            argv,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return CommandResult(done.returncode, done.stdout + done.stderr, False, False)

    def close(self) -> None:
        return None


@dataclass
class Checkpoints:
    """Checkpoint 0 is the original; each write saves the whole workspace's text files."""

    run: SandboxRun | None = None
    saved: list[dict[str, str]] = field(default_factory=list)

    def _snapshot(self, workspace: Workspace) -> dict[str, str]:
        files = {}
        for path in sorted(workspace.path.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(workspace.path).as_posix()
                text = workspace.read_text(relative)
                if text is not None:
                    files[relative] = text
        return files

    def on_write(self, _path: str) -> int:
        assert self.run is not None
        if not self.saved:
            self.saved.append({})
        self.saved.append(self._snapshot(self.run.workspace))
        return len(self.saved) - 1

    def rollback(self, action: Action) -> JsonValue:
        assert self.run is not None
        number = action.args.get("checkpoint")
        if not isinstance(number, int) or not 0 < number < len(self.saved):
            return {"error": "no such checkpoint"}
        restored: list[JsonValue] = []
        for relative, text in self.saved[number].items():
            if self.run.workspace.read_text(relative) != text:
                self.run.workspace.write_file(relative, text)
                restored.append(relative)
        return {"restored": restored}


def demo_run(
    tmp_path: Path, *, plan: Manifest | None = DEMO_PLAN
) -> tuple[SandboxRun, Checkpoints]:
    checkpoints = Checkpoints()
    run = SandboxRun(
        DEMO_REPO,
        engine=engine(),
        runner_factory=LocalRunner,
        on_write=checkpoints.on_write,
        tools={"rollback": checkpoints.rollback},
        temp_root=tmp_path,
    )
    checkpoints.run = run
    if plan is not None:
        run.add_layer(PlanLayer(plan, engine().rulebook.context))
    return run, checkpoints
