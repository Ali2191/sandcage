"""The sample event stream for the panel's mock API, **recorded from the real harness**.

The demo run (Architecture/Run walkthrough) goes through :class:`Harness` with the scripted
models, a scripted human who approves the plan and resumes after the breaker, a real
:class:`SandboxRun` with the real gate, and a fixed clock, so the stream is the same on
every machine. Only the command runner is replayed: instead of running pytest it returns
pytest's output for the code as it stands (the failing discount test before the fix, all
tests passing after), so timings can't make the file change between runs. Every gate
verdict, rule, blocked sentence, plan, verdict, review and report field comes from the
real code.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Final

from sandcage.agent.scenarios import BUGGY_LINE, DEFAULT_TASK, ScenarioId, scripted_models
from sandcage.contract.approval import Approval
from sandcage.contract.events import Event
from sandcage.demo_repos import INVOICE_CALC
from sandcage.harness.checkpoints import Checkpoints
from sandcage.harness.guards import Guards, Limits, RunClock
from sandcage.harness.pipeline import Answer, Harness, Models, RunOptions
from sandcage.policy import PolicyEngine
from sandcage.roles.reader import QuarantinedReader
from sandcage.sandbox.container import CommandResult
from sandcage.sandbox.run import SandboxRun

RUN_ID: Final = "3f2a9c1e7b04"
START: Final = 1_793_000_000.0
TICK: Final = 0.25
_FAILING: Final = (
    "....F\n"
    "FAILED tests/test_invoice.py::test_discount_is_a_percentage - assert 90 == 99\n"
    "1 failed, 4 passed in 0.02s\n"
)
_PASSING: Final = ".....\n5 passed in 0.02s\n"


class _Clock:
    """Every reading moves time on by a fixed tick: deterministic, and always increasing."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        self.now = round(self.now + TICK, 3)
        return self.now


class _ReplayRunner:
    """pytest's output for the workspace as it is; nothing else is run."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def start(self) -> None:
        return None

    def run(self, command: str) -> CommandResult:
        if command.split()[:1] != ["pytest"] and "pytest" not in command:
            return CommandResult(0, "", False, False)
        code = (self.workspace / "invoice_calc" / "invoice.py").read_text(encoding="utf-8")
        failing = BUGGY_LINE in code
        return CommandResult(1 if failing else 0, _FAILING if failing else _PASSING, False, False)

    def close(self) -> None:
        return None


def _approve(approval: Approval) -> Answer:
    return Answer(approval.resolve(next(c.id for c in approval.choices if c.approves)))


def demo_events() -> list[Event]:
    """The demo run, event by event, with ids 1..n as the store would number them."""
    scripted = scripted_models(ScenarioId.DEMO)
    models = Models(
        understand=scripted.understand,
        scout=scripted.scout,
        planner=scripted.planner,
        executor=scripted.executor,
        verifier=scripted.verifier,
        redteam=scripted.redteam,
        reader=scripted.reader,
    )
    clock = _Clock()
    engine = PolicyEngine.from_file()
    checkpoints = Checkpoints(INVOICE_CALC, run_id=RUN_ID, clock=clock)
    reader = QuarantinedReader(scripted.reader)
    events: list[Event] = []
    with tempfile.TemporaryDirectory(prefix="sandcage-sample-") as temp:
        run = SandboxRun(
            INVOICE_CALC,
            engine=engine,
            runner_factory=_ReplayRunner,
            on_write=checkpoints.on_write,
            tools={"rollback": checkpoints.rollback},
            reader=reader,
            temp_root=Path(temp),
        )
        with run:
            limits = Limits()
            harness = Harness(
                run_id=RUN_ID,
                models=models,
                run=run,
                original=INVOICE_CALC,
                engine=engine,
                options=RunOptions(task=DEFAULT_TASK, repo="invoice-calc", limits=limits),
                checkpoints=checkpoints,
                reader=reader,
                human=_approve,
                sink=lambda event: events.append(event.model_copy(update={"id": len(events) + 1})),
                clock=clock,
                guards=Guards(RUN_ID, limits, clock=RunClock(clock), wall=clock),
            )
            harness.execute()
    return events
