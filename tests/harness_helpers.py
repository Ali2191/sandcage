"""Run a scenario through the whole harness with scripted models and a scripted human."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sandcage.agent.scenarios import DEFAULT_TASK, ScenarioId, scripted_models
from sandcage.agent.scenarios import Models as ScriptedModels
from sandcage.contract.approval import Approval, ApprovalKind, Resolution
from sandcage.contract.events import Event
from sandcage.harness.checkpoints import Checkpoints
from sandcage.harness.pipeline import Answer, Harness, HarnessResult, Models, RunOptions
from sandcage.roles.reader import QuarantinedReader
from sandcage.sandbox.run import GateMode, RunnerFactory, SandboxRun
from tests.agent_helpers import LocalRunner
from tests.policy_helpers import engine
from tests.sandbox_helpers import DEMO_REPO

CLARIFY_ANSWER = "A percentage: 10 means 10% off."


def models_of(scripted: ScriptedModels) -> Models:
    return Models(
        understand=scripted.understand,
        scout=scripted.scout,
        planner=scripted.planner,
        executor=scripted.executor,
        verifier=scripted.verifier,
        redteam=scripted.redteam,
        reader=scripted.reader,
    )


def approve(approval: Approval) -> Resolution:
    if approval.free_text is not None:
        return approval.resolve("answer", CLARIFY_ANSWER)
    return approval.resolve(next(c.id for c in approval.choices if c.approves))


def refuse(approval: Approval) -> Resolution:
    return approval.resolve(next(c.id for c in approval.choices if not c.approves))


@dataclass
class ScriptedHuman:
    """Approves everything unless told otherwise per approval kind; can run a callback
    (for example a stop) while a question is open, like a human acting mid-wait."""

    answers: dict[ApprovalKind, Callable[[Approval], Resolution]] = field(default_factory=dict)
    during: dict[ApprovalKind, Callable[[], object]] = field(default_factory=dict)
    asked: list[Approval] = field(default_factory=list)

    def __call__(self, approval: Approval) -> Answer:
        self.asked.append(approval)
        hook = self.during.get(approval.kind)
        if hook is not None:
            hook()
        return Answer(self.answers.get(approval.kind, approve)(approval))


@dataclass
class Run:
    result: HarnessResult
    harness: Harness
    models: ScriptedModels
    human: ScriptedHuman
    streamed: list[Event]
    workspace_files: dict[str, str]

    def kinds(self) -> list[str]:
        return [event.kind for event in self.result.events]


def run_scenario(
    tmp_path: Path,
    scenario: ScenarioId | str = ScenarioId.DEMO,
    *,
    human: ScriptedHuman | None = None,
    runner_factory: RunnerFactory = LocalRunner,
    mode: GateMode = GateMode.ENFORCE,
    guard: bool = True,
    models: ScriptedModels | None = None,
    options: RunOptions | None = None,
    prepare: Callable[[Harness], None] | None = None,
) -> Run:
    tmp_path.mkdir(parents=True, exist_ok=True)
    scripted = models or scripted_models(scenario)
    person = human or ScriptedHuman()
    checkpoints = Checkpoints(DEMO_REPO, run_id="harness0001")
    reader = QuarantinedReader(scripted.reader)
    run = SandboxRun(
        DEMO_REPO,
        engine=engine(),
        runner_factory=runner_factory,
        mode=mode,
        on_write=checkpoints.on_write,
        tools={"rollback": checkpoints.rollback},
        reader=reader,
        temp_root=tmp_path,
    )
    streamed: list[Event] = []
    with run:
        harness = Harness(
            run_id="harness0001",
            models=models_of(scripted),
            run=run,
            original=DEMO_REPO,
            engine=engine(),
            options=options or RunOptions(task=DEFAULT_TASK, guard=guard),
            checkpoints=checkpoints,
            reader=reader,
            human=person,
            sink=streamed.append,
        )
        if prepare is not None:
            prepare(harness)
        result = harness.execute()
        files = {
            path: text
            for path in ("invoice_calc/invoice.py", "tests/test_invoice.py")
            if (text := run.read_text(path)) is not None
        }
    return Run(result, harness, scripted, person, streamed, files)
