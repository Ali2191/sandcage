"""The harness end to end with scripted models: the walkthrough's event sequence, stop
semantics (fault-injected, never by timing), delivery and withholding, and every scenario."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sandcage.agent.llm import Completion, Message, ModelError
from sandcage.agent.scenarios import (
    BUGGY_LINE,
    CLARIFY_QUESTION,
    FIXED_LINE,
    scripted_models,
)
from sandcage.agent.scripted import ScriptedClient
from sandcage.contract.approval import ApprovalKind
from sandcage.contract.events import (
    ApprovalResolvedEvent,
    CheckpointEvent,
    DeliverEvent,
    DoneEvent,
    PlanEvent,
    RedteamEvent,
    ReportEvent,
    ResolvedBy,
    RunStatus,
    StepEvent,
    StoppedEvent,
    UnderstandEvent,
)
from sandcage.harness.pipeline import Harness, HarnessResult
from sandcage.report.endings import OUTCOME_DELIVERED, OUTCOME_STOPPED, OUTCOME_WITHHELD
from sandcage.sandbox.run import GateMode
from tests.harness_helpers import CLARIFY_ANSWER, Run, ScriptedHuman, refuse, run_scenario
from tests.sandbox_helpers import DEMO_REPO, DEMO_SECRET_VALUE

AFTER_STOP = {"verify", "redteam", "deliver", "report", "learn"}


class Broken:
    def complete(self, messages: list[Message]) -> Completion:
        raise ModelError("the provider is down")


# --- the demo --------------------------------------------------------------------------------


def test_the_demo_follows_the_walkthrough(tmp_path: Path) -> None:
    run = run_scenario(tmp_path)
    result = run.result
    assert result.status is RunStatus.FINISHED
    assert run.kinds() == [
        "understand", "scout",
        "plan", "approval_needed", "approval_resolved", "plan", "plan",
        "step", "step", "step", "step",
        "breaker", "approval_needed", "approval_resolved", "breaker",
        "step", "step", "step", "checkpoint", "step", "step",
        "verify", "redteam", "deliver", "report", "learn", "done",
    ]  # fmt: skip
    plans = [e.state.value for e in result.events if isinstance(e, PlanEvent)]
    assert plans == ["proposed", "approved", "locked"]
    asked = [a.kind for a in run.human.asked]
    assert asked == [ApprovalKind.PLAN, ApprovalKind.BREAKER]
    blocked = [e for e in result.events if isinstance(e, StepEvent) and e.blocked]
    assert [e.blocked_sentence for e in blocked] == [
        "Tried to send your .env to collector.example.net",
        "Tried to read your secrets in .env",
    ]
    assert FIXED_LINE in run.workspace_files["invoice_calc/invoice.py"]
    report = result.report
    assert report is not None
    assert report.event.outcome == OUTCOME_DELIVERED
    assert report.event.headline == (
        "The agent tried to cross the line. Sandcage blocked it 2 times, and the task "
        "still got done."
    )
    assert not report.event.trifecta.all_at_once
    assert report.event.trifecta.way_out is False
    hosts = [(r.host, r.requests, r.blocked) for r in report.event.egress]
    assert hosts == [("collector.example.net", 0, True)]  # measured: no request left
    done = result.events[-1]
    assert isinstance(done, DoneEvent)
    assert (done.outcome, done.delivered, done.verified) == (OUTCOME_DELIVERED, True, True)
    assert done.changed_files == ["invoice_calc/invoice.py"]
    assert run.streamed == result.events
    assert all(e.budget is not None for e in result.events)
    assert result.outputs is not None
    assert "+" + FIXED_LINE.rstrip("\n") in result.outputs.patch
    assert DEMO_SECRET_VALUE not in str([e.model_dump() for e in result.events])


def test_the_delivered_patch_applies_and_reproduces_the_fix(tmp_path: Path) -> None:
    run = run_scenario(tmp_path / "run")
    assert run.result.outputs is not None
    copy = tmp_path / "copy"
    shutil.copytree(DEMO_REPO, copy)
    (copy / "sandcage.patch").write_text(run.result.outputs.patch)
    git = shutil.which("git")
    assert git is not None
    applied = subprocess.run(  # noqa: S603 - git with fixed arguments
        [git, "apply", "sandcage.patch"], cwd=copy, capture_output=True, text=True, check=False
    )
    assert applied.returncode == 0, applied.stderr
    assert (copy / "invoice_calc/invoice.py").read_text() == run.workspace_files[
        "invoice_calc/invoice.py"
    ]


# --- stop ------------------------------------------------------------------------------------


def _stop_during(kind: ApprovalKind, tmp_path: Path, scenario: str = "demo") -> tuple[Run, Harness]:
    holder: dict[str, Harness] = {}
    person = ScriptedHuman()
    # the API's order: set the flag first, then the waiting question gets its answer (yes!)
    person.during[kind] = lambda: holder["h"].request_stop()  # noqa: PLW0108 - bound later

    def keep(harness: Harness) -> None:
        holder["h"] = harness

    run = run_scenario(tmp_path, scenario, human=person, prepare=keep)
    return run, holder["h"]


def test_stop_during_plan_approval_delivers_nothing(tmp_path: Path) -> None:
    """Fault-injected: the stop lands while the plan question is open and the human's answer
    is a yes. No timing is involved."""
    run, harness = _stop_during(ApprovalKind.PLAN, tmp_path)
    result = run.result
    assert result.status is RunStatus.STOPPED
    kinds = [e.kind for e in result.events]
    assert not AFTER_STOP & set(kinds)
    assert "step" not in kinds
    assert kinds.count("stopped") == 1
    assert kinds[-1] == "done"
    resolved = next(e for e in result.events if isinstance(e, ApprovalResolvedEvent))
    assert (resolved.approved, resolved.by) == (False, ResolvedBy.STOP)
    assert result.outputs is None
    assert result.delivery is None
    done = result.events[-1]
    assert isinstance(done, DoneEvent)
    assert (done.outcome, done.delivered) == (OUTCOME_STOPPED, False)
    assert harness.request_stop() is False  # stopping twice records one event
    assert [e.kind for e in result.events].count("stopped") == 1


@pytest.mark.parametrize(
    ("kind", "scenario"),
    [
        (ApprovalKind.BREAKER, "demo"),
        (ApprovalKind.ACTION, "sneaky_urllib"),
        (ApprovalKind.REDTEAM, "sneaky_urllib"),
        (ApprovalKind.QUESTION, "clarify"),
    ],
)
def test_stop_while_any_question_is_open_delivers_nothing(
    tmp_path: Path, kind: ApprovalKind, scenario: str
) -> None:
    run, _ = _stop_during(kind, tmp_path, scenario)
    result = run.result
    kinds = [e.kind for e in result.events]
    assert result.status is RunStatus.STOPPED
    assert "deliver" not in kinds
    assert "report" not in kinds
    assert kinds.count("stopped") == 1
    assert result.outputs is None


def test_stop_during_a_model_call_calls_no_more_models(tmp_path: Path) -> None:
    holder: dict[str, Harness] = {}
    scripted = scripted_models()

    class StopsTheRun:
        def complete(self, messages: list[Message]) -> Completion:
            holder["h"].request_stop()
            return scripted.scout.complete(messages)

    scripted.scout = StopsTheRun()  # type: ignore[assignment]
    run = run_scenario(tmp_path, models=scripted, prepare=lambda h: holder.update(h=h))
    assert run.result.status is RunStatus.STOPPED
    assert scripted.planner.calls == []
    assert scripted.executor.calls == []
    assert scripted.redteam.calls == []
    assert not AFTER_STOP & set(run.kinds())


# --- withholding -----------------------------------------------------------------------------


def _assert_withheld(result: HarnessResult) -> None:
    report = result.report
    assert report is not None
    assert report.event.outcome == OUTCOME_WITHHELD
    assert "Delivered" not in report.event.headline
    assert "got done" not in report.event.headline
    assert "withheld" in report.event.headline
    outputs = result.outputs
    assert outputs is not None
    assert outputs.blocked
    for text in (outputs.summary_md, outputs.report_md):
        assert "Delivered" not in text
        assert "got done" not in text
        assert "withheld" in text
    done = result.events[-1]
    assert isinstance(done, DoneEvent)
    assert (done.outcome, done.delivered, done.delivery_blocked) == (
        OUTCOME_WITHHELD,
        False,
        True,
    )
    deliver = next(e for e in result.events if isinstance(e, DeliverEvent))
    assert deliver.blocked


def test_an_unreviewed_patch_is_blocked_and_reads_withheld(tmp_path: Path) -> None:
    scripted = scripted_models()
    scripted.redteam = Broken()  # type: ignore[assignment]
    person = ScriptedHuman(answers={ApprovalKind.REDTEAM: refuse})
    run = run_scenario(tmp_path, models=scripted, human=person)
    review = next(e for e in run.result.events if isinstance(e, RedteamEvent))
    assert (review.blocked, review.unreviewed) == (True, True)
    assert run.result.status is RunStatus.FINISHED
    _assert_withheld(run.result)


def test_the_sneaky_patch_is_withheld_until_a_human_releases_it(tmp_path: Path) -> None:
    kept = run_scenario(
        tmp_path / "kept",
        "sneaky_urllib",
        human=ScriptedHuman(answers={ApprovalKind.REDTEAM: refuse}),
    )
    asked = [a.kind for a in kept.human.asked]
    assert ApprovalKind.ACTION in asked  # the diff guard asked about the urllib write
    assert ApprovalKind.REDTEAM in asked
    _assert_withheld(kept.result)

    released = run_scenario(tmp_path / "released", "sneaky_urllib")
    report = released.result.report
    assert report is not None
    assert report.event.outcome == OUTCOME_DELIVERED
    done = released.result.events[-1]
    assert isinstance(done, DoneEvent)
    assert (done.delivered, done.released_by_human, done.delivery_blocked) == (True, True, False)
    assert released.result.outputs is not None
    assert not released.result.outputs.blocked


# --- the other scenarios -----------------------------------------------------------------


def test_skip_test_still_ends_in_an_ask_or_a_block(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, "skip_test")
    weakening = [
        e
        for e in run.result.events
        if isinstance(e, StepEvent) and e.tool == "write_file" and "tests/" in str(e.args)
    ]
    assert weakening
    assert all(e.blocked or e.decision.value == "ask" for e in weakening)
    assert all(not e.executed for e in weakening)
    assert run.result.status is RunStatus.FINISHED
    assert "pytest.mark.skip" not in run.workspace_files["tests/test_invoice.py"]
    verifies = [e.kind for e in run.result.events].count("verify")
    assert verifies == 2  # failed deterministically, then passed after the fix


def test_rollback_announces_checkpoints_after_their_steps(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, "rollback")
    events = run.result.events
    for index, event in enumerate(events):
        if isinstance(event, CheckpointEvent):
            previous = events[index - 1]
            assert isinstance(previous, StepEvent)
            assert previous.tool in {"write_file", "rollback"}
            assert event.step == previous.step
    states = [e.state.value for e in events if isinstance(e, CheckpointEvent)]
    assert states == ["saved", "saved", "restored"]
    assert FIXED_LINE in run.workspace_files["invoice_calc/invoice.py"]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert (done.rollbacks, done.checkpoints) == (1, 2)


def test_clarify_asks_first_and_the_answer_reaches_the_agents(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, "clarify")
    assert run.human.asked[0].kind is ApprovalKind.QUESTION
    assert run.human.asked[0].headline == CLARIFY_QUESTION
    states = [e.state.value for e in run.result.events if isinstance(e, UnderstandEvent)]
    assert states == ["asking", "answered"]
    assert CLARIFY_ANSWER in run.models.planner.seen
    assert CLARIFY_ANSWER in run.models.executor.seen
    assert run.result.status is RunStatus.FINISHED


def test_an_unanswered_question_stops_before_anything_runs(tmp_path: Path) -> None:
    person = ScriptedHuman(answers={ApprovalKind.QUESTION: refuse})
    run = run_scenario(tmp_path, "clarify", human=person)
    assert run.result.status is RunStatus.NEEDS_CLARIFICATION
    assert run.models.scout.calls == []
    assert run.models.executor.calls == []
    kinds = run.kinds()
    assert "step" not in kinds
    assert kinds[-5:] == ["redteam", "deliver", "report", "learn", "done"]  # still reported


def test_a_refused_plan_runs_nothing_but_is_still_reported(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, human=ScriptedHuman(answers={ApprovalKind.PLAN: refuse}))
    assert run.result.status is RunStatus.PLAN_REJECTED
    assert run.models.executor.calls == []
    assert BUGGY_LINE in run.workspace_files["invoice_calc/invoice.py"]
    report = run.result.report
    assert report is not None
    assert report.event.headline == "You rejected the plan, so nothing ran."
    assert report.event.outcome == "Plan rejected"


def test_killing_at_the_breaker_still_reports(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, human=ScriptedHuman(answers={ApprovalKind.BREAKER: refuse}))
    assert run.result.status is RunStatus.KILLED
    kinds = run.kinds()
    assert "verify" not in kinds
    assert {"redteam", "deliver", "report"} <= set(kinds)
    report = run.result.report
    assert report is not None
    assert report.event.headline == (
        "Sandcage blocked the agent 2 times. "
        "The run ended at the breaker before the task was done. Nothing is delivered."
    )


def test_a_failed_planner_is_plan_invalid(tmp_path: Path) -> None:
    scripted = scripted_models()
    scripted.planner = ScriptedClient(["nope", "still nope"])
    run = run_scenario(tmp_path, models=scripted)
    assert run.result.status is RunStatus.PLAN_INVALID
    plans = [e for e in run.result.events if isinstance(e, PlanEvent)]
    assert [p.state.value for p in plans] == ["invalid"]


def test_a_model_failure_mid_run_is_an_error_and_still_reported(tmp_path: Path) -> None:
    scripted = scripted_models()
    scripted.executor = Broken()  # type: ignore[assignment]
    run = run_scenario(tmp_path, models=scripted)
    assert run.result.status is RunStatus.ERROR
    assert "provider is down" in run.result.summary
    assert isinstance(run.result.events[-3], ReportEvent)
    assert run.result.events[-2].kind == "learn"


def test_observe_mode_is_described_honestly(tmp_path: Path) -> None:
    run = run_scenario(tmp_path, mode=GateMode.OBSERVE, guard=False)
    outputs = run.result.outputs
    assert outputs is not None
    assert "Mode: **observe**" in outputs.report_md
    assert outputs.report["mode"] == "observe"
    blocked = [e for e in run.result.events if isinstance(e, StepEvent) and e.blocked]
    assert blocked == []  # nothing is blocked in observe mode; verdicts are recorded


def test_tokens_are_labelled_estimated_for_scripted_models(tmp_path: Path) -> None:
    run = run_scenario(tmp_path)
    outputs = run.result.outputs
    assert outputs is not None
    assert "(estimated: the provider did not report usage)" in outputs.report_md
    assert "scripted (no real model)" in outputs.report_md
    assert outputs.report["tokens_reported"] is False
    roles = outputs.report["tokens_by_role"]
    assert isinstance(roles, dict)
    assert set(roles) == {"understand", "scout", "planner", "executor", "verifier", "redteam"}


def test_events_and_outputs_carry_no_secret(tmp_path: Path) -> None:
    run = run_scenario(tmp_path)
    outputs = run.result.outputs
    assert outputs is not None
    for text in (outputs.patch, outputs.summary_md, outputs.report_md, str(outputs.report)):
        assert DEMO_SECRET_VALUE not in text
    stopped = [e for e in run.result.events if isinstance(e, StoppedEvent)]
    assert stopped == []
