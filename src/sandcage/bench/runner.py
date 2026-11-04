"""The one scenario runner.

The Bench, the end-to-end tests and the Attack gallery all start a scenario the same way:
build its repo in a temp directory (:func:`sandcage.bench.repo.materialize`), take its task
and its scripted model set from the catalogue, and run it through the real harness. This
module is the Bench's side of that: one scenario, one mode, judged on the finished,
still-open workspace and the run's event log.

A crash anywhere in a run becomes a failed result with the error text; it never aborts the
suite and never goes missing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from sandcage.bench import repo
from sandcage.bench.catalogue import CLARIFY_ANSWER, SCENARIOS
from sandcage.bench.predicates import Evidence, Rules, attack_succeeded
from sandcage.bench.results import ModeResult, ScenarioResult
from sandcage.bench.scenario import TOKEN, Mode, Scenario
from sandcage.contract.approval import Approval, ApprovalKind, Resolution
from sandcage.contract.events import Event, StepEvent
from sandcage.harness.checkpoints import Checkpoints
from sandcage.harness.pipeline import Answer, Harness, Models, RunOptions
from sandcage.masking import mask
from sandcage.policy import Decision, PolicyEngine
from sandcage.roles.reader import QuarantinedReader
from sandcage.sandbox.run import GateMode, RunnerFactory, SandboxRun

LOG = logging.getLogger(__name__)
TEST_COMMAND: Final = "pytest -q"
ERROR_LIMIT: Final = 300
APPROVED_KINDS: Final = frozenset({ApprovalKind.PLAN, ApprovalKind.BREAKER})


def reviewer(approval: Approval) -> Answer:
    """The Bench's cautious reviewer: approves the plan and resumes after a breaker trip,
    answers a clarifying question, and refuses every other ask, amendment and blocked patch."""
    if approval.kind is ApprovalKind.QUESTION and approval.free_text is not None:
        return Answer(approval.resolve("answer", CLARIFY_ANSWER))
    if approval.kind in APPROVED_KINDS:
        return Answer(approval.resolve(next(c.id for c in approval.choices if c.approves)))
    refusal: Resolution = approval.resolve(next(c.id for c in approval.choices if not c.approves))
    return Answer(refusal)


def rules_of(engine: PolicyEngine) -> Rules:
    rulebook = engine.rulebook
    return Rules(
        guard=rulebook.diff_guard,
        test_files=rulebook.context.test_files,
        secret_globs=tuple(rulebook.context.secrets.globs),
    )


def models_for(scenario: Scenario) -> Models:
    """The scripted model set for a scenario, shaped for the harness (shared with the API)."""
    scripted = scenario.models(repo.repo_files(scenario))
    return Models(
        understand=scripted.understand,
        scout=scripted.scout,
        planner=scripted.planner,
        executor=scripted.executor,
        verifier=scripted.verifier,
        redteam=scripted.redteam,
        reader=scripted.reader,
    )


def run_mode(
    scenario: Scenario,
    mode: Mode,
    *,
    runner_factory: RunnerFactory,
    engine: PolicyEngine | None = None,
    temp_root: Path | None = None,
    models: Models | None = None,
) -> ModeResult:
    """Run one scenario in one mode and judge it. Never raises: a crash is a failed result."""
    chosen = engine or PolicyEngine.from_file()
    root: Path | None = None
    try:
        root = repo.materialize(scenario, temp_root)
        return _run(
            scenario,
            mode,
            root,
            engine=chosen,
            runner_factory=runner_factory,
            temp_root=temp_root,
            models=models,
        )
    except Exception as problem:  # noqa: BLE001 - spec: a crash is a failed result, not a stop
        text = mask(f"{type(problem).__name__}: {problem}")[:ERROR_LIMIT]
        LOG.warning("bench run %s/%s crashed: %s", scenario.id, mode.value, text)
        return ModeResult(mode, None, None, "error", 0, 0, 0, error=text)
    finally:
        if root is not None:
            repo.remove(root)


def _run(
    scenario: Scenario,
    mode: Mode,
    root: Path,
    *,
    engine: PolicyEngine,
    runner_factory: RunnerFactory,
    temp_root: Path | None,
    models: Models | None,
) -> ModeResult:
    full = mode is Mode.FULL
    run_id = f"bench-{scenario.id}"[:24]
    checkpoints = Checkpoints(root, run_id=run_id)
    scripted = models or models_for(scenario)
    reader = QuarantinedReader(scripted.reader)
    events: list[Event] = []
    sandbox = SandboxRun(
        root,
        engine=engine,
        runner_factory=runner_factory,
        mode=GateMode.ENFORCE if full else GateMode.OBSERVE,
        on_write=checkpoints.on_write,
        tools={"rollback": checkpoints.rollback},
        reader=reader,
        temp_root=temp_root,
    )
    with sandbox:
        harness = Harness(
            run_id=run_id,
            models=scripted,
            run=sandbox,
            original=root,
            engine=engine,
            options=RunOptions(task=scenario.task, repo=scenario.id, guard=full),
            checkpoints=checkpoints,
            reader=reader,
            human=reviewer,
            sink=events.append,
        )
        result = harness.execute()
        evidence = Evidence(
            original=_visible_original(root, sandbox.workspace.left_out),
            final=repo.read_tree(sandbox.workspace.path),
            events=result.events,
            secret=_secret_of(root),
        )
        tests = sandbox.run_harness_command(TEST_COMMAND)
    judged = attack_succeeded(scenario.predicate, evidence, rules_of(engine))
    steps = evidence.steps
    denied = sum(1 for s in steps if s.decision is Decision.DENY)
    asked = sum(1 for s in steps if s.decision is Decision.ASK)
    completed = tests.exit_code == 0 and not tests.timed_out
    if not evidence.complete:
        return ModeResult(
            mode, None, None, result.status.value, len(steps), denied, asked,
            error="the run left nothing to judge",
        )  # fmt: skip
    note = _benign_note(scenario, mode, steps, denied + asked)
    return ModeResult(
        mode, judged, completed, result.status.value, len(steps), denied, asked, note=note
    )


def _benign_note(
    scenario: Scenario, mode: Mode, steps: Sequence[StepEvent], flagged: int
) -> str | None:
    if scenario.benign and mode is Mode.FULL and flagged:
        return f"a benign task was stopped or asked about {flagged} time(s)"
    return None


def _visible_original(root: Path, left_out: Sequence[str]) -> dict[str, str]:
    """The original files the sandbox copy actually had: files left out of the copy (secret
    files in the full mode) are not changes."""
    return {
        path: text
        for path, text in repo.read_tree(root).items()
        if not any(path == gone or path.startswith(gone + "/") for gone in left_out)
    }


def _secret_of(root: Path) -> str:
    env = repo.read_tree(root).get(".env", "")
    return TOKEN if TOKEN in env else ""


def run_scenario(
    scenario: Scenario,
    modes: Sequence[Mode],
    *,
    runner_factory: RunnerFactory,
    engine: PolicyEngine | None = None,
    temp_root: Path | None = None,
) -> ScenarioResult:
    found = {
        mode: run_mode(
            scenario, mode, runner_factory=runner_factory, engine=engine, temp_root=temp_root
        )
        for mode in modes
    }
    return ScenarioResult(scenario, found.get(Mode.BASELINE), found.get(Mode.FULL))


def run_suite(
    *,
    runner_factory: RunnerFactory,
    modes: Sequence[Mode] = (Mode.BASELINE, Mode.FULL),
    only: Sequence[str] = (),
    scenarios: Sequence[Scenario] = SCENARIOS,
    temp_root: Path | None = None,
    progress: Callable[[str], object] = lambda _line: None,
) -> list[ScenarioResult]:
    """Every selected scenario in every selected mode, in catalogue order."""
    engine = PolicyEngine.from_file()
    chosen = [s for s in scenarios if not only or s.id in only]
    results: list[ScenarioResult] = []
    for scenario in chosen:
        started = time.monotonic()
        result = run_scenario(
            scenario, modes, runner_factory=runner_factory, engine=engine, temp_root=temp_root
        )
        results.append(result)
        progress(f"{scenario.id}: done in {time.monotonic() - started:.1f}s")
    return results
