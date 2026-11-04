"""Guards: budgets (human wait excluded), the shared token meter, loop detection and the
breaker, with exact threshold tests, unit-level and through the real agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sandcage.agent.llm import Completion, Message, Usage
from sandcage.agent.loop import Agent
from sandcage.agent.protocol import parse_action
from sandcage.agent.scenarios import FINISH, ScenarioId, act, scripted_models
from sandcage.agent.scripted import ScriptedClient
from sandcage.contract.approval import Approval, ApprovalKind, Resolution, breaker_approval
from sandcage.contract.common import Phase
from sandcage.contract.events import (
    BreakerEvent,
    BreakerState,
    EventBase,
    GuardEvent,
    RunStatus,
    StepEvent,
)
from sandcage.harness.guards import (
    Breaker,
    GuardConfigError,
    Guards,
    Limits,
    LoopDetector,
    MeteredClient,
    RunClock,
    RunStoppedError,
    TokenMeter,
    estimate_tokens,
)
from sandcage.policy import Decision, Risk
from tests.agent_helpers import IS_SECRET, demo_run
from tests.test_agent import FakeExecutor

TAG = "prompt_injection_suspected"
LS = act("list_dir", "look", path=".")
TEST = act("run_command", "test", command="pytest -q")
WRITE = act("write_file", "fix", path="a.py", content="x = 1\n")
CURL = act("run_command", "x", command="curl https://evil.example")


@dataclass
class FakeTime:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Recorder:
    events: list[EventBase] = field(default_factory=list)

    def __call__(self, event: EventBase) -> None:
        self.events.append(event)

    def of(self, kind: type[EventBase]) -> list[EventBase]:
        return [e for e in self.events if isinstance(e, kind)]


def _step(
    step: int, *, blocked: bool = True, tags: list[str] | None = None, **extra: object
) -> StepEvent:
    fields: dict[str, object] = {
        "run_id": "r",
        "phase": Phase.ACT,
        "ts": 0.0,
        "step": step,
        "guard": True,
        "tool": "run_command",
        "args": {},
        "decision": Decision.DENY if blocked else Decision.ALLOW,
        "rule_id": "net.deny_tools",
        "reason": "no network",
        "risk": Risk.HIGH,
        "tags": tags or [],
        "executed": not blocked,
        "blocked": blocked,
        "blocked_sentence": "Tried to send your .env to collector.example.net" if blocked else None,
    }
    fields.update(extra)
    return StepEvent.model_validate(fields)


# --- limits ----------------------------------------------------------------------------------


def test_defaults_and_environment_overrides() -> None:
    assert Limits() == Limits(25, 600.0, 150_000)
    assert Limits.from_env({}) == Limits()
    limits = Limits.from_env({"SANDCAGE_MAX_SECONDS": "90", "LLM_MAX_TOKENS": " 5000 "})
    assert (limits.max_seconds, limits.max_tokens) == (90.0, 5000)
    assert limits.capped(1000).max_tokens == 1000
    assert limits.capped(99_999).max_tokens == 5000  # a cap only lowers
    for bad in ("0", "-5", "nan", "inf", "lots"):
        with pytest.raises(GuardConfigError, match="positive"):
            Limits.from_env({"SANDCAGE_MAX_SECONDS": bad})
    with pytest.raises(GuardConfigError):
        Limits.from_env({"LLM_MAX_TOKENS": "1.5"})


# --- time ------------------------------------------------------------------------------------


def test_human_waiting_does_not_count() -> None:
    now = FakeTime()
    clock = RunClock(now)
    assert clock.elapsed() == 0.0
    clock.start()
    now.now += 10
    with clock.waiting():
        now.now += 100
        assert clock.elapsed() == 10  # while waiting, too
        with clock.waiting():  # nested waits count once
            now.now += 50
    now.now += 5
    assert clock.elapsed() == 15


def test_time_budget_fires_at_the_limit_and_excludes_approvals() -> None:
    now = FakeTime()
    events = Recorder()
    guards = Guards("r", Limits(max_seconds=60), clock=RunClock(now), emit=events)
    guards.start()
    now.now += 59.9
    assert guards.gate(None) is None

    def slow_human(approval: Approval) -> Resolution:
        now.now += 500
        return approval.timeout_resolution()

    approval = breaker_approval(1, source=None, tried=[])
    guards.wait_for_human(slow_human)(approval)
    assert guards.gate(None) is None
    now.now += 0.1
    assert guards.gate(None) is RunStatus.BUDGET_TIME
    guard_events = events.of(GuardEvent)
    assert len(guard_events) == 1
    assert isinstance(guard_events[0], GuardEvent)
    assert guard_events[0].stop_reason == "budget.time"
    assert guards.gate(None) is RunStatus.BUDGET_TIME
    assert len(events.of(GuardEvent)) == 1  # announced once


# --- tokens ----------------------------------------------------------------------------------


def test_the_meter_prefers_reported_usage_and_flags_estimates() -> None:
    meter = TokenMeter()
    added: list[int] = []
    meter.listeners.append(added.append)
    meter.add("executor", Usage(100, 20), 999, 999)
    assert (meter.total, meter.prompt, meter.completion, meter.reported) == (120, 100, 20, True)
    meter.add("scout", None, 401, 8)
    assert estimate_tokens(401) == 101
    assert meter.total == 120 + 101 + 2
    assert meter.reported is False
    assert meter.by_role == {"executor": 120, "scout": 103}
    assert added == [120, 103]


def test_every_role_shares_one_meter_and_the_token_budget() -> None:
    guards = Guards("r", Limits(max_tokens=300))
    guards.start()
    executor = guards.metered(ScriptedClient([LS], usage=Usage(100, 50)), "executor")
    scout = guards.metered(ScriptedClient(["{}"], usage=Usage(100, 49)), "scout")
    messages: list[Message] = [{"role": "user", "content": "x"}]
    executor.complete(messages)
    scout.complete(messages)
    assert guards.meter.by_role == {"executor": 150, "scout": 149}
    assert guards.gate(None) is None  # 299 < 300
    guards.metered(ScriptedClient(["{}"], usage=Usage(1, 0)), "verifier").complete(messages)
    assert guards.gate(None) is RunStatus.BUDGET_TOKENS


def test_a_stopped_run_never_calls_a_model() -> None:
    guards = Guards("r")
    inner = ScriptedClient([LS])
    client: MeteredClient = guards.metered(inner, "redteam")
    guards.request_stop()
    with pytest.raises(RunStoppedError):
        client.complete([{"role": "user", "content": "x"}])
    assert inner.calls == []
    assert guards.gate(None) is RunStatus.STOPPED


# --- loops -----------------------------------------------------------------------------------


def test_the_third_identical_proposal_is_a_loop_not_the_second() -> None:
    loops = LoopDetector()
    ls = parse_action(LS)
    assert loops.propose(ls) is False
    assert loops.propose(parse_action(TEST)) is False  # need not be consecutive
    assert loops.propose(ls) is False
    assert loops.propose(ls) is True


def test_progress_forgets_everything_but_writes() -> None:
    loops = LoopDetector()
    test = parse_action(TEST)
    for attempt in range(6):  # fix-then-test with a new fix each time is not a loop
        fix = parse_action(act("write_file", "fix", path="a.py", content=f"y = {attempt}\n"))
        assert loops.propose(fix) is False
        assert loops.propose(test) is False
        loops.progress()
    write = parse_action(WRITE)
    assert [loops.propose(write), loops.propose(test)] == [False, False]
    loops.progress()
    assert loops.propose(write) is False
    loops.progress()
    assert loops.propose(write) is True  # the same content written three times


def test_alternating_actions_are_a_loop_and_feedback_counts_as_progress() -> None:
    guards = Guards("r")
    guards.start()
    ls, test = parse_action(LS), parse_action(TEST)
    assert [guards.gate(a) for a in (ls, test, ls, test)] == [None] * 4
    assert guards.gate(ls) is RunStatus.LOOP_REPEAT
    guards = Guards("r")
    assert [guards.gate(a) for a in (ls, ls)] == [None, None]
    guards.progress()  # verifier feedback
    assert guards.gate(ls) is None


def test_a_loop_stops_before_the_third_runs_in_the_agent() -> None:
    executor = FakeExecutor()
    events = Recorder()
    guards = Guards("r", emit=events)
    guards.start()
    agent = Agent(
        client=ScriptedClient([TEST, TEST, TEST], when_done=FINISH),
        executor=executor,
        run_id="r",
        is_secret=IS_SECRET,
        hooks=guards.hooks(),
    )
    result = agent.run_task("fix it")
    assert result.status is RunStatus.LOOP_REPEAT
    assert len(executor.ran) == 2
    guard_event = events.of(GuardEvent)[0]
    assert isinstance(guard_event, GuardEvent)
    assert "proposed 3 times" in guard_event.detail


def test_executed_writes_are_progress_in_the_agent() -> None:
    guards = Guards("r")
    guards.start()
    script = [TEST, TEST, WRITE, TEST, TEST, FINISH]
    agent = Agent(
        client=ScriptedClient(script),
        executor=FakeExecutor(),
        run_id="r",
        is_secret=IS_SECRET,
        hooks=guards.hooks(),
    )
    assert agent.run_task("fix it").status is RunStatus.FINISHED


# --- the breaker -----------------------------------------------------------------------------


def test_three_denials_trip_not_two() -> None:
    breaker = Breaker()
    assert [breaker.record(_step(1)), breaker.record(_step(2))] == [False, False]
    assert breaker.record(_step(3, blocked=False)) is False  # allowed steps don't count
    assert breaker.record(_step(4)) is True
    assert (breaker.denials, breaker.trips) == (3, 1)


def test_two_injection_denials_trip_not_one() -> None:
    breaker = Breaker()
    assert breaker.record(_step(1, tags=[TAG], source="README.md")) is False
    assert breaker.record(_step(2, tags=[TAG], source="README.md")) is True
    assert breaker.source() == "README.md"
    assert "possible prompt injection" in breaker.reason()


def test_observed_steps_and_refused_amendments_do_not_count() -> None:
    breaker = Breaker()
    for step in range(5):
        assert breaker.record(_step(step, observed=True)) is False
        assert breaker.record(_step(step, tool="amend_plan")) is False
    assert breaker.denials == 0
    unapproved_ask = _step(9, decision=Decision.ASK, approved=False)
    assert breaker.record(unapproved_ask) is False
    assert breaker.denials == 1


def test_resume_resets_counts_and_no_answer_kills() -> None:
    asked: list[Approval] = []

    def resume(approval: Approval) -> Resolution:
        asked.append(approval)
        return approval.resolve("resume")

    events = Recorder()
    guards = Guards("r", approver=resume, emit=events)
    guards.start()
    for step in (1, 2):
        assert guards.after_step(_step(step)) is None
    assert guards.after_step(_step(3)) is None  # tripped, resumed
    assert guards.breaker.denials == 0
    assert guards.breaker.trips == 1
    states = [e.state for e in events.of(BreakerEvent) if isinstance(e, BreakerEvent)]
    assert states == [BreakerState.TRIPPED, BreakerState.RESUMED]
    assert asked[0].kind is ApprovalKind.BREAKER

    for approver in (None, lambda a: a.timeout_resolution(), _raises):
        guards = Guards("r", approver=approver)
        for step in (1, 2):
            guards.after_step(_step(step))
        assert guards.after_step(_step(3)) is RunStatus.KILLED


def _raises(approval: Approval) -> Resolution:
    raise RuntimeError("panel gone")


def test_a_stop_during_the_breaker_question_wins() -> None:
    guards = Guards("r")

    def stop_then_yes(approval: Approval) -> Resolution:
        guards.request_stop()
        return approval.resolve("resume")

    guards.approver = stop_then_yes
    guards.after_step(_step(1, tags=[TAG]))
    assert guards.after_step(_step(2, tags=[TAG])) is RunStatus.STOPPED


def test_the_breaker_wait_is_excluded_from_time() -> None:
    now = FakeTime()

    def slow(approval: Approval) -> Resolution:
        now.now += 1000
        return approval.resolve("resume")

    guards = Guards("r", Limits(max_seconds=60), approver=slow, clock=RunClock(now))
    guards.start()
    guards.after_step(_step(1, tags=[TAG]))
    guards.after_step(_step(2, tags=[TAG]))
    assert guards.gate(None) is None


def test_annotate_adds_budget_and_the_count_including_this_step() -> None:
    guards = Guards("r", Limits(max_steps=10))
    guards.start()
    guards.meter.add("executor", None, 40, 0)
    event = guards.annotate(_step(4, tags=[TAG]))
    assert event.budget is not None
    assert (event.budget.steps, event.budget.max_steps, event.budget.tokens) == (4, 10, 10)
    assert event.budget.tokens_reported is False
    assert event.breaker is not None
    assert (event.breaker.denials, event.breaker.injection_denials) == (1, 1)


def test_the_demo_trips_the_breaker_with_the_s3_approval_record(tmp_path: Path) -> None:
    """The scripted demo through a real SandboxRun: the curl and the .env read are both
    injection-tagged from README.md, so the breaker trips on the second; its banner is the
    one ``breaker_approval`` record with Sandcage's own sentences; resume lets the fix land."""
    run, _ = demo_run(tmp_path)
    models = scripted_models(ScenarioId.DEMO)
    asked: list[Approval] = []

    def resume(approval: Approval) -> Resolution:
        asked.append(approval)
        return approval.resolve("resume")

    events = Recorder()
    guards = Guards("demo00000001", approver=resume, emit=events)
    guards.start()
    with run:
        run.session.mark_suspicious("README.md")
        run.session.mark_suspicious("invoice_calc/invoice.py")
        agent = Agent(
            client=guards.metered(models.executor, "executor"),
            executor=run,
            run_id="demo00000001",
            is_secret=IS_SECRET,
            hooks=guards.hooks(),
            emit=events,
        )
        result = agent.run_task("Set up the project, fix the test.")
    assert result.status is RunStatus.FINISHED
    assert len(asked) == 1
    assert asked[0] == breaker_approval(
        asked[0].step,
        source="README.md",
        tried=["send your .env to collector.example.net", "read your secrets in .env"],
    )
    assert asked[0].headline == (
        "Something in README.md is steering the agent, so Sandcage paused it."
    )
    kinds = [type(e).__name__ for e in events.events]
    tripped_at = kinds.index("BreakerEvent")
    assert kinds[tripped_at - 1] == "StepEvent"  # the step comes first, then the trip
    steps = [e for e in events.events if isinstance(e, StepEvent)]
    assert all(e.budget is not None and e.breaker is not None for e in steps)
    assert guards.meter.reported is False  # the scripted model reports no usage
    assert guards.meter.by_role["executor"] > 0


def test_kill_at_the_breaker_ends_the_demo_before_the_fix(tmp_path: Path) -> None:
    run, _ = demo_run(tmp_path)
    models = scripted_models(ScenarioId.DEMO)
    guards = Guards("demo00000001", approver=lambda a: a.resolve("end"))
    guards.start()
    with run:
        run.session.mark_suspicious("README.md")
        agent = Agent(
            client=models.executor,
            executor=run,
            run_id="demo00000001",
            is_secret=IS_SECRET,
            hooks=guards.hooks(),
        )
        result = agent.run_task("Set up the project, fix the test.")
        fixed = run.read_text("invoice_calc/invoice.py")
    assert result.status is RunStatus.KILLED
    assert fixed is not None
    assert "return amount - percent" in fixed


def test_scripted_client_usage_reaches_the_meter() -> None:
    guards = Guards("r")
    client = guards.metered(ScriptedClient(["{}"], usage=Usage(3, 4)), "planner")
    completion: Completion = client.complete([{"role": "user", "content": "hi"}])
    assert completion.text == "{}"
    assert guards.meter.total == 7
    assert guards.meter.reported is True
