"""How every run ends, in words (S8 step 0, decided 4 Nov).

- One backend catalogue gives every end status a pill label and a headline; the tables here
  are a deliberate snapshot (change them only on purpose).
- A run is "delivered" only when it finished with the checks passing and the red-team gate
  cleared (or a human released the patch). Every unfinished status ends undelivered, and
  never reads "Delivered" or "the task got done".
- Changes left behind by an unfinished run are withheld, and a human can still release them.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sandcage.agent.scenarios import FINISH, ScenarioId, act, scripted_models
from sandcage.agent.scenarios import Models as ScriptedModels
from sandcage.agent.scripted import ScriptedClient
from sandcage.contract.approval import ApprovalKind
from sandcage.contract.events import DoneEvent, RunStatus
from sandcage.harness.guards import Limits
from sandcage.harness.pipeline import RunOptions
from sandcage.report.endings import ENDINGS, OUTCOME_WITHHELD
from sandcage.report.report import headline, outcome
from tests.harness_helpers import Run, ScriptedHuman, refuse, run_scenario

ALL_STATUSES = list(RunStatus)

# (label, headline when nothing is held back, headline when changes are withheld)
SNAPSHOT: dict[RunStatus, tuple[str, str, str]] = {
    RunStatus.RUNNING: ("Running", "The run is still going.", "The run is still going."),
    RunStatus.FINISHED: (
        "Delivered",
        "The run finished inside the cage, and the task got done.",
        "The run finished inside the cage, but the patch is withheld until you review it.",
    ),
    RunStatus.VERIFY_FAILED: (
        "Not verified",
        "The agent stopped, but the checks did not pass. Nothing is delivered.",
        "The agent stopped, but the checks did not pass, so the changes are withheld until you "
        "review them.",
    ),
    RunStatus.NEEDS_CLARIFICATION: (
        "Needs an answer",
        "The agent needed an answer from you and did not get one, so nothing ran.",
        "The agent needed an answer from you and did not get one, so nothing ran.",
    ),
    RunStatus.PLAN_INVALID: (
        "No safe plan",
        "Sandcage could not make a plan it could lock for this task, so nothing ran.",
        "Sandcage could not make a plan it could lock for this task, so nothing ran.",
    ),
    RunStatus.PLAN_REJECTED: (
        "Plan rejected",
        "You rejected the plan, so nothing ran.",
        "You rejected the plan, so nothing ran.",
    ),
    RunStatus.BUDGET_STEPS: (
        "Out of steps",
        "The agent used all its steps before the task was done. Nothing is delivered.",
        "The agent used all its steps before the task was done, so the changes are withheld "
        "until you review them.",
    ),
    RunStatus.BUDGET_TOKENS: (
        "Out of tokens",
        "The run used its token limit before the task was done. Nothing is delivered.",
        "The run used its token limit before the task was done, so the changes are withheld "
        "until you review them.",
    ),
    RunStatus.BUDGET_TIME: (
        "Out of time",
        "The run used its time limit before the task was done. Nothing is delivered.",
        "The run used its time limit before the task was done, so the changes are withheld "
        "until you review them.",
    ),
    RunStatus.LOOP_REPEAT: (
        "Stuck in a loop",
        "The agent kept repeating the same action, so Sandcage ended the run. "
        "Nothing is delivered.",
        "The agent kept repeating the same action, so Sandcage ended the run, so the changes "
        "are withheld until you review them.",
    ),
    RunStatus.KILLED: (
        "Ended at the breaker",
        "The run ended at the breaker before the task was done. Nothing is delivered.",
        "The run ended at the breaker before the task was done, so the changes are withheld "
        "until you review them.",
    ),
    RunStatus.INVALID_REPLY: (
        "Model gave up",
        "The model sent replies Sandcage could not use, so the run ended. Nothing is delivered.",
        "The model sent replies Sandcage could not use, so the run ended, so the changes are "
        "withheld until you review them.",
    ),
    RunStatus.ERROR: (
        "Run failed",
        "The run failed before the task was done. Nothing is delivered.",
        "The run failed before the task was done, so the changes are withheld until you review "
        "them.",
    ),
    RunStatus.STOPPED: (
        "Stopped by you",
        "You stopped this run. Nothing left the cage.",
        "You stopped this run. Nothing left the cage.",
    ),
    RunStatus.INTERRUPTED: (
        "Interrupted",
        "The API restarted while this run was going, so it was cut off. Nothing is delivered.",
        "The API restarted while this run was going, so it was cut off, so the changes are "
        "withheld until you review them.",
    ),
}


# --- the catalogue --------------------------------------------------------------------------


def test_every_status_has_an_entry() -> None:
    assert set(ENDINGS) == set(ALL_STATUSES)
    assert set(SNAPSHOT) == set(ALL_STATUSES)


def test_wording_snapshot() -> None:
    table = {
        status: (
            outcome(status, withheld=False),
            headline(status, 0, withheld=False),
            headline(status, 0, withheld=True),
        )
        for status in ALL_STATUSES
    }
    for status in (RunStatus.RUNNING, RunStatus.NEEDS_CLARIFICATION, RunStatus.PLAN_REJECTED):
        assert table[status][1] == table[status][2]  # nothing can be withheld there
    assert table == SNAPSHOT


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_no_pill_or_headline_is_a_raw_status(status: RunStatus) -> None:
    for withheld in (False, True):
        pill = outcome(status, withheld=withheld)
        line = headline(status, 0, withheld=withheld)
        assert pill != status.value
        raw = "_" in status.value or "." in status.value  # prose may say "stopped"
        assert not raw or status.value not in pill + line
        assert "_" not in pill
        assert pill == pill.strip()
        assert line == line.strip()
        assert line.endswith(".")


@pytest.mark.parametrize("status", [s for s in ALL_STATUSES if s is not RunStatus.FINISHED])
def test_an_unfinished_run_never_reads_delivered_or_done(status: RunStatus) -> None:
    for withheld in (False, True):
        for blocked in (0, 1, 3):
            pill = outcome(status, withheld=withheld)
            line = headline(status, blocked, withheld=withheld)
            assert pill != "Delivered"
            assert "got done" not in line
            assert "Delivered" not in line


def test_a_withheld_unfinished_run_reads_withheld_with_the_reason() -> None:
    assert outcome(RunStatus.VERIFY_FAILED, withheld=True) == OUTCOME_WITHHELD
    assert "checks did not pass" in headline(RunStatus.VERIFY_FAILED, 0, withheld=True)


def test_blocked_count_prefixes_an_unfinished_headline() -> None:
    assert headline(RunStatus.VERIFY_FAILED, 2, withheld=False) == (
        "Sandcage blocked the agent 2 times. "
        "The agent stopped, but the checks did not pass. Nothing is delivered."
    )


# --- through the harness: each unfinished status --------------------------------------------


def _with_executor(scenario: ScenarioId, replies: list[str]) -> ScriptedModels:
    models = scripted_models(scenario)
    return replace(models, executor=ScriptedClient(replies, when_done=FINISH))


def _done(run: Run) -> DoneEvent:
    done = run.result.events[-1]
    assert isinstance(done, DoneEvent)
    return done


def _assert_undelivered(run: Run, status: RunStatus) -> DoneEvent:
    assert run.result.status is status
    done = _done(run)
    assert done.delivered is False
    report = run.result.report
    assert report is not None
    assert report.event.outcome != "Delivered"
    assert "got done" not in report.event.headline
    assert "Delivered" not in report.event.headline
    assert report.markdown.startswith("# Sandcage run report\n\n**")
    assert "**Delivered." not in report.markdown
    outputs = run.result.outputs
    assert outputs is not None
    assert "**Delivered." not in outputs.summary_md
    return done


def test_verify_failed_with_no_changes_is_not_verified_and_not_delivered(tmp_path: Path) -> None:
    """The S7 live case: the agent stops without fixing anything, the checks fail."""
    run = run_scenario(tmp_path, models=_with_executor(ScenarioId.DEMO, [FINISH]))
    done = _assert_undelivered(run, RunStatus.VERIFY_FAILED)
    assert (done.outcome, done.delivery_blocked, done.verified) == ("Not verified", False, False)
    assert done.released_by_human is False
    report = run.result.report
    assert report is not None
    assert report.event.headline == (
        "The agent stopped, but the checks did not pass. Nothing is delivered."
    )
    assert report.data["released_by_human"] is False


def test_verify_failed_with_changes_withholds_them_until_a_human_releases(tmp_path: Path) -> None:
    wrong = act(
        "write_file",
        "A different fix",
        path="invoice_calc/invoice.py",
        content=_wrong_source(),
    )
    run = run_scenario(tmp_path, models=_with_executor(ScenarioId.DEMO, [wrong, FINISH]))
    done = _assert_undelivered(run, RunStatus.VERIFY_FAILED)
    assert (done.outcome, done.delivery_blocked) == (OUTCOME_WITHHELD, True)
    report = run.result.report
    assert report is not None
    assert "checks did not pass, so the changes are withheld" in report.event.headline
    outputs = run.result.outputs
    assert outputs is not None
    assert outputs.blocked is True
    assert "did not finish with the checks passing" in outputs.summary_md
    # no question during the run: for an unfinished run the report is where a human decides
    assert ApprovalKind.REDTEAM not in {a.kind for a in run.human.asked}


def test_a_rejected_plan_says_so_and_delivers_nothing(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, human=ScriptedHuman(answers={ApprovalKind.PLAN: refuse}))
    done = _assert_undelivered(run, RunStatus.PLAN_REJECTED)
    assert done.outcome == "Plan rejected"
    report = run.result.report
    assert report is not None
    assert report.event.headline == "You rejected the plan, so nothing ran."


def test_running_out_of_steps_withholds_the_unverified_changes(tmp_path: Path) -> None:
    run = run_scenario(
        tmp_path,
        ScenarioId.ROLLBACK,
        options=RunOptions(task="Fix the discount", limits=Limits(max_steps=2)),
    )
    done = _assert_undelivered(run, RunStatus.BUDGET_STEPS)
    assert (done.outcome, done.delivery_blocked) == (OUTCOME_WITHHELD, True)


def test_running_out_of_steps_with_no_changes_reads_out_of_steps(tmp_path: Path) -> None:
    run = run_scenario(
        tmp_path,
        options=RunOptions(task="Fix the discount", limits=Limits(max_steps=1)),
    )
    done = _assert_undelivered(run, RunStatus.BUDGET_STEPS)
    assert (done.outcome, done.delivery_blocked) == ("Out of steps", False)


def test_running_out_of_tokens_is_not_delivered(tmp_path: Path) -> None:
    run = run_scenario(
        tmp_path,
        options=RunOptions(task="Fix the discount", limits=Limits(max_tokens=1)),
    )
    assert run.result.status in {RunStatus.BUDGET_TOKENS, RunStatus.PLAN_INVALID}
    assert _done(run).delivered is False


def test_ending_at_the_breaker_is_not_delivered(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, human=ScriptedHuman(answers={ApprovalKind.BREAKER: refuse}))
    done = _assert_undelivered(run, RunStatus.KILLED)
    assert done.outcome in {"Ended at the breaker", OUTCOME_WITHHELD}


def test_a_finished_verified_run_is_still_delivered(tmp_path: Path) -> None:
    run = run_scenario(tmp_path)
    assert run.result.status is RunStatus.FINISHED
    done = _done(run)
    assert (done.delivered, done.outcome, done.verified) == (True, "Delivered", True)


# --- helpers -------------------------------------------------------------------------------


def _wrong_source() -> str:
    from sandcage.agent.scenarios import WRONG_LINE, fixed_source

    return fixed_source(WRONG_LINE)
