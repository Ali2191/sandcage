"""A host runner for Bench tests: ``pytest``, ``ls`` and ``cat`` as in the agent tests, and
``rm`` limited to files inside the workspace copy (the Bench has a scenario that deletes a
file). Anything else, ``curl`` included, is "not available here" with exit code 127: it is
still *dispatched*, which is what the command predicate judges. Tests only; production
runs in Docker."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from sandcage.sandbox.container import CommandResult
from tests.agent_helpers import LocalRunner


@dataclass
class BenchRunner(LocalRunner):
    workspace: Path
    commands: list[str] = field(default_factory=list)

    def run(self, command: str) -> CommandResult:
        argv = shlex.split(command)
        if argv[:1] != ["rm"]:
            return super().run(command)
        self.commands.append(command)
        for name in (word for word in argv[1:] if not word.startswith("-")):
            target = (self.workspace / name).resolve()
            if self.workspace.resolve() not in target.parents:
                return CommandResult(1, f"rm: {name}: refused\n", False, False)
            target.unlink(missing_ok=True)
        return CommandResult(0, "", False, False)
