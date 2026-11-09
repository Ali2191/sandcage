"""The run manager over the real SQLite store: a run per thread, approvals answered by
approval id + choice id, stop ordering (fault-injected at every approval kind and inside a
model call, never by timing), the slot freed before ``done``, recovery, rewind, release,
learn-accept and admission."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sandcage.agent.llm import Completion, Message
from sandcage.agent.scenarios import scripted_models
from sandcage.api.errors import ConflictError, InvalidError, NotFoundError, UnavailableError
from sandcage.api.guard import RefusalError
from sandcage.api.runs import BusyError, RunManager, StartRequest
from sandcage.api.settings import DemoSettings, ProviderSettings
from sandcage.contract.approval import Approval, ApprovalKind
from sandcage.contract.common import Phase
from sandcage.contract.events import (
    ApprovalNeededEvent,
    ApprovalResolvedEvent,
    CheckpointEvent,
    DoneEvent,
    Event,
    ResolvedBy,
    RunStatus,
    StoppedEvent,
    UnknownEvent,
)
from sandcage.events.store import EventStore, Outputs, utc_day
from sandcage.harness.pipeline import Models
from tests.api_helpers import TASK, WAIT, Choice, Env, answers, make_env, refuse

AFTER_STOP = {"verify", "redteam", "deliver", "report", "learn"}


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


# --- a whole run ---------------------------------------------------------------------------


def test_the_demo_runs_on_a_thread_into_the_real_store(env: Env) -> None:
    run_id = env.start()
    seen = env.drive(run_id)
    assert [a.kind for a in env.asked] == [ApprovalKind.PLAN, ApprovalKind.BREAKER]
    kinds = env.kinds(run_id)
    assert kinds[-6:] == ["verify", "redteam", "deliver", "report", "learn", "done"]
    assert [e.kind for e in seen] == kinds
    ids = [e.id for e in env.store.events(run_id)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    record = env.store.get_run(run_id)
    assert record.status is RunStatus.FINISHED
    assert (record.repo, record.task, record.provider, record.model) == (
        "invoice-calc",
        TASK,
        "scripted",
        None,
    )
    assert record.done is not None
    assert record.done.delivered is True
    outputs = env.store.outputs(run_id)
    assert outputs is not None
    assert "+    return amount - amount * percent / Decimal(100)" in outputs.patch
    resolved = [e for e in seen if isinstance(e, ApprovalResolvedEvent)]
    assert [r.by for r in resolved] == [ResolvedBy.HUMAN, ResolvedBy.HUMAN]
    assert env.manager.active_ids() == []


def test_tokens_are_counted_in_the_day_and_on_the_row(env: Env) -> None:
    run_id = env.start()
    env.drive(run_id)
    record = env.store.get_run(run_id)
    assert record.done is not None
    assert record.done.budget is not None
    spent = record.done.budget.tokens
    assert spent > 0
    assert record.tokens == spent
    day = env.store.daily_usage(_today(env))
    assert (day.runs, day.tokens) == (1, spent)


def _today(env: Env) -> str:
    return utc_day(env.store.get_run(env.store.list_runs()[0][0].id).created)


# --- approvals ------------------------------------------------------------------------------


def test_approve_validates_against_the_pending_approval(env: Env) -> None:
    run_id = env.start()
    problems: list[type[Exception]] = []

    def answer(approval: Approval) -> Choice | None:
        for wrong in (
            ("not-the-id", approval.choices[0].id, None),
            (approval.id, "no-such-choice", None),
            (approval.id, approval.choices[0].id, "a written answer to a plan"),
        ):
            try:
                env.manager.approve(run_id, *wrong)
            except (ConflictError, InvalidError) as problem:
                problems.append(type(problem))
        return Choice(next(c.id for c in approval.choices if c.approves))

    env.drive(run_id, answer)
    assert problems == [ConflictError, InvalidError, InvalidError] * 2
    with pytest.raises(ConflictError):  # nothing pending after the run
        env.manager.approve(run_id, "x", "allow")
    with pytest.raises(NotFoundError):
        env.manager.approve("ffffffffffff", "x", "allow")


def test_an_answer_cannot_be_given_twice(env: Env) -> None:
    run_id = env.start()
    doubled: list[str] = []

    def answer(approval: Approval) -> Choice | None:
        yes = next(c.id for c in approval.choices if c.approves)
        env.manager.approve(run_id, approval.id, yes)
        try:
            env.manager.approve(run_id, approval.id, yes)
        except ConflictError:
            doubled.append(approval.kind.value)
        return None  # already answered

    env.drive(run_id, answer)
    assert doubled == ["plan", "breaker"]
    assert env.store.get_run(run_id).status is RunStatus.FINISHED


def test_an_unanswered_approval_times_out_as_the_record_says(tmp_path: Path) -> None:
    env = make_env(tmp_path, approval_timeout=0.05)
    run_id = env.start()
    seen = env.drive(run_id, lambda approval: None)
    resolved = [e for e in seen if isinstance(e, ApprovalResolvedEvent)]
    assert [(r.approval_kind, r.by, r.approved) for r in resolved] == [
        (ApprovalKind.PLAN, ResolvedBy.TIMEOUT, False)
    ]
    assert env.store.get_run(run_id).status is RunStatus.PLAN_REJECTED
    assert "step" not in env.kinds(run_id)


def test_clarify_waits_for_the_answer_and_passes_it_on(env: Env) -> None:
    run_id = env.start("clarify")
    env.drive(run_id)
    assert env.asked[0].kind is ApprovalKind.QUESTION
    assert env.store.get_run(run_id).status is RunStatus.FINISHED
    resolved = [e for e in env.store.events(run_id) if isinstance(e, ApprovalResolvedEvent)]
    assert resolved[0].answer == "A percentage: 10 means 10% off."


# --- stop -----------------------------------------------------------------------------------


def _stop_at(env: Env, kind: ApprovalKind, scenario: str) -> str:
    run_id = env.start(scenario)
    statuses: list[object] = []

    def answer(approval: Approval) -> Choice | None:
        if approval.kind is kind:
            statuses.append(env.manager.stop(run_id))
            return None
        return Choice(next(c.id for c in approval.choices if c.approves))

    env.drive(run_id, answer)
    assert statuses == [RunStatus.STOPPED]
    return run_id


@pytest.mark.parametrize(
    ("kind", "scenario"),
    [
        (ApprovalKind.PLAN, "demo"),
        (ApprovalKind.BREAKER, "demo"),
        (ApprovalKind.ACTION, "sneaky_urllib"),
        (ApprovalKind.REDTEAM, "sneaky_urllib"),
        (ApprovalKind.QUESTION, "clarify"),
    ],
)
def test_stop_from_every_approval_state_delivers_nothing(
    env: Env, kind: ApprovalKind, scenario: str
) -> None:
    run_id = _stop_at(env, kind, scenario)
    kinds = env.kinds(run_id)
    after = kinds[kinds.index("stopped") :]
    assert not AFTER_STOP & set(after)
    assert kinds.count("stopped") == 1
    assert kinds[-1] == "done"
    record = env.store.get_run(run_id)
    assert record.status is RunStatus.STOPPED
    assert record.done is not None
    assert (record.done.delivered, record.done.outcome) == (False, "Stopped by you")
    assert env.store.outputs(run_id) is None
    resolved = [e for e in env.store.events(run_id) if isinstance(e, ApprovalResolvedEvent)]
    assert resolved[-1].by is ResolvedBy.STOP
    assert resolved[-1].approval_kind is kind
    assert resolved[-1].approved is False
    events = env.store.events(run_id)
    stopped = next(i for i, e in enumerate(events) if isinstance(e, StoppedEvent))
    answered = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, ApprovalResolvedEvent) and e.by is ResolvedBy.STOP
    )
    assert stopped < answered  # the flag (and its event) before the waiting answer


def test_stop_twice_records_one_event_and_a_finished_run_is_unchanged(env: Env) -> None:
    run_id = env.start()
    second: list[object] = []

    def answer(approval: object) -> Choice | None:
        env.manager.stop(run_id)
        second.append(env.manager.stop(run_id))
        return None

    env.drive(run_id, answer)
    assert second == [RunStatus.STOPPED]
    assert env.kinds(run_id).count("stopped") == 1
    before = env.kinds(run_id)
    assert env.manager.stop(run_id) is RunStatus.STOPPED
    assert env.kinds(run_id) == before
    with pytest.raises(NotFoundError):
        env.manager.stop("ffffffffffff")


class Gate:
    """A chat client that blocks inside its first call until released (a slow model)."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def complete(self, messages: list[Message]) -> Completion:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(WAIT)
        complete = getattr(self.inner, "complete")  # noqa: B009 - a duck-typed client
        result: Completion = complete(messages)
        return result


def _models_with(role: str, gate: Gate) -> Models:
    scripted = scripted_models("demo")
    clients = {name: getattr(scripted, name) for name in Models.__dataclass_fields__}
    clients[role] = gate
    return Models(**clients)


def test_stop_during_a_model_call_calls_no_more_models(tmp_path: Path) -> None:
    holder: dict[str, Gate] = {}

    def models(request: StartRequest) -> Models:
        gate = Gate(scripted_models("demo").scout)
        holder["gate"] = gate
        return _models_with("scout", gate)

    env = make_env(tmp_path, models_factory=models, stop_wait=0.0)
    run_id = env.start()
    gate = _wait_gate(holder)
    assert env.manager.stop(run_id) == "stopping"  # still inside the model call
    gate.release.set()
    assert env.manager.wait(run_id, WAIT)
    kinds = env.kinds(run_id)
    assert "plan" not in kinds
    assert not AFTER_STOP & set(kinds)
    assert kinds.count("stopped") == 1
    assert env.store.get_run(run_id).status is RunStatus.STOPPED
    assert gate.calls == 1


def _wait_gate(holder: dict[str, Gate]) -> Gate:
    for _ in range(int(WAIT * 100)):
        if "gate" in holder:
            break
        threading.Event().wait(0.01)
    gate = holder["gate"]
    assert gate.entered.wait(WAIT)
    return gate


def test_stop_before_the_harness_exists_still_ends_with_one_stop(tmp_path: Path) -> None:
    started = threading.Event()
    proceed = threading.Event()

    def models(request: StartRequest) -> Models:
        started.set()
        assert proceed.wait(WAIT)
        return _models_with("scout", Gate(scripted_models("demo").scout))

    env = make_env(tmp_path, models_factory=models, stop_wait=0.0)
    run_id = env.start()
    assert started.wait(WAIT)
    assert env.manager.stop(run_id) == "stopping"
    assert env.manager.stop(run_id) == "stopping"
    proceed.set()
    assert env.manager.wait(run_id, WAIT)
    assert env.kinds(run_id) == ["stopped", "done"]
    record = env.store.get_run(run_id)
    assert record.status is RunStatus.STOPPED
    assert env.manager.active_ids() == []


def test_a_failure_before_the_harness_ends_as_error_with_done(tmp_path: Path) -> None:
    def models(request: StartRequest) -> Models:
        raise RuntimeError("no models today")

    env = make_env(tmp_path, models_factory=models)
    run_id = env.start()
    assert env.manager.wait(run_id, WAIT)
    assert env.kinds(run_id) == ["done"]
    record = env.store.get_run(run_id)
    assert record.status is RunStatus.ERROR
    assert record.done is not None
    assert "no models today" in record.done.summary
    assert env.manager.active_ids() == []


# --- slots and admission --------------------------------------------------------------------


def test_the_slot_is_free_before_done_is_visible(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        demo=DemoSettings(max_concurrent=1, runs_per_client_per_hour=100, runs_per_day=100),
    )
    first = env.start()
    with pytest.raises(BusyError) as busy:
        env.start()
    assert first in busy.value.message
    started: list[str] = []

    def on_event(event: Event | UnknownEvent) -> None:
        if isinstance(event, DoneEvent):
            started.append(env.start())  # the instant done is visible

    env.drive(first, on_event=on_event)
    assert len(started) == 1
    env.drive(started[0])


def test_admission_is_atomic_under_concurrent_starts(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        demo=DemoSettings(max_concurrent=5, runs_per_client_per_hour=100, runs_per_day=1),
    )
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    lock = threading.Lock()

    def go() -> None:
        barrier.wait()
        try:
            env.start()
            result = "started"
        except RefusalError as refused:
            result = str(refused.status)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert sorted(outcomes) == ["429", "429", "429", "started"]
    for run_id in env.manager.active_ids():
        env.manager.stop(run_id)


def test_start_refuses_what_it_cannot_run(env: Env, tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        env.manager.start(StartRequest(repo="../../etc", task=TASK), client="x")
    with pytest.raises(InvalidError):
        env.start("no_such_scenario")
    real = make_env(tmp_path / "real", provider=ProviderSettings(provider="vultr", model="m"))
    with pytest.raises(UnavailableError):
        real.manager.start(StartRequest(repo="invoice-calc", task=TASK), client="x")
    keyed = ProviderSettings(provider="vultr", api_key="k", model="m")
    with pytest.raises(InvalidError):  # a scripted run on a real provider: known ids only
        make_env(tmp_path / "keyed", provider=keyed).start("no_such_scenario")
    assert env.store.list_runs()[1] == 0


# --- rewind, release, learn ------------------------------------------------------------------


def test_a_human_rewind_is_applied_while_acting(env: Env) -> None:
    run_id = env.start()
    asked: list[str] = []

    def answer(approval: Approval) -> Choice | None:
        if approval.kind is ApprovalKind.PLAN:
            with pytest.raises(ConflictError):
                env.manager.rewind(run_id, 0)  # not acting yet
        if approval.kind is ApprovalKind.BREAKER:
            assert env.manager.acting(run_id)
            with pytest.raises(InvalidError):
                env.manager.rewind(run_id, 7)
            env.manager.rewind(run_id, 0)
            asked.append("rewound")
        return Choice(next(c.id for c in approval.choices if c.approves))

    env.drive(run_id, answer)
    assert asked == ["rewound"]
    restored = [
        e
        for e in env.store.events(run_id)
        if isinstance(e, CheckpointEvent) and e.restored_by == "human"
    ]
    assert len(restored) == 1
    assert restored[0].checkpoint == 0
    with pytest.raises(ConflictError):
        env.manager.rewind(run_id, 0)  # the run is over
    listed = env.manager.checkpoints(run_id)
    assert listed[0] == {"id": 0, "step": None, "files": []}


def test_a_withheld_patch_is_released_and_the_row_says_so(env: Env) -> None:
    run_id = env.start("sneaky_urllib")
    env.drive(run_id, answers({ApprovalKind.REDTEAM: refuse}))
    record = env.store.get_run(run_id)
    assert record.done is not None
    assert (record.done.delivery_blocked, record.released_by_human) == (True, False)
    env.manager.release(run_id)
    assert env.store.get_run(run_id).released_by_human is True
    outputs = env.store.outputs(run_id)
    assert outputs is not None
    assert not outputs.blocked
    with pytest.raises(ConflictError):
        env.manager.release(run_id)


def test_release_without_a_withheld_patch_is_a_conflict(env: Env) -> None:
    run_id = env.start()
    env.drive(run_id)
    with pytest.raises(ConflictError):
        env.manager.release(run_id)


def test_learn_suggestions_can_be_accepted_once(env: Env) -> None:
    run_id = env.start()
    env.drive(run_id)
    learned = env.manager.learn(run_id)
    assert learned is not None
    assert learned.suggestions
    first = learned.suggestions[0]
    rule = env.manager.accept(run_id, first.id)
    assert rule.id == first.rule_id
    assert (env.tmp / "data" / "learned.yaml").exists()
    with pytest.raises(ConflictError):
        env.manager.accept(run_id, first.id)
    with pytest.raises(NotFoundError):
        env.manager.accept(run_id, "no-such-suggestion")


# --- recovery -------------------------------------------------------------------------------


def test_restart_marks_running_runs_interrupted_and_ends_their_streams(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "sandcage.db")
    store.create_run("aaaaaaaaaaaa", repo="invoice-calc", task="t", guard=True)
    store.create_run("bbbbbbbbbbbb", repo="invoice-calc", task="t", guard=True)
    finished = DoneEvent(
        run_id="bbbbbbbbbbbb",
        phase=Phase.REPORT,
        ts=1.0,
        status=RunStatus.FINISHED,
        summary="done",
        steps=0,
        guard=True,
    )
    store.finish_run("bbbbbbbbbbbb", finished, tokens=0)
    env = make_env(tmp_path, store=store)
    assert env.manager.recover() == ["aaaaaaaaaaaa"]
    record = store.get_run("aaaaaaaaaaaa")
    assert record.status is RunStatus.INTERRUPTED
    stream = store.wait_for_events("aaaaaaaaaaaa", after_id=0, timeout=1)
    assert [e.kind for e in stream] == ["done"]
    done = stream[0]
    assert isinstance(done, DoneEvent)
    assert "Nothing was delivered" in done.summary
    assert store.get_run("bbbbbbbbbbbb").status is RunStatus.FINISHED
    assert env.manager.recover() == []  # recovered once
    run_id = env.start()  # new runs start normally after a restart
    env.drive(run_id)
    assert store.get_run(run_id).status is RunStatus.FINISHED


def test_a_run_cut_off_after_delivering_keeps_its_patch(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "sandcage.db")
    store.create_run("aaaaaaaaaaaa", repo="invoice-calc", task="t", guard=True)
    store.save_outputs("aaaaaaaaaaaa", Outputs("--- a\n+++ b\n", "# s", False, {}, "# r", {}))
    env = make_env(tmp_path, store=store)
    env.manager.recover()
    record = store.get_run("aaaaaaaaaaaa")
    assert record.done is not None
    assert "can still be downloaded" in record.done.summary
    assert record.done.delivered is True
    assert store.outputs("aaaaaaaaaaaa") is not None


def test_recovery_refuses_while_runs_are_active(tmp_path: Path) -> None:
    holder: dict[str, Gate] = {}

    def models(request: StartRequest) -> Models:
        gate = Gate(scripted_models("demo").scout)
        holder["gate"] = gate
        return _models_with("scout", gate)

    env = make_env(tmp_path, models_factory=models, stop_wait=0.0)
    run_id = env.start()
    gate = _wait_gate(holder)
    with pytest.raises(ConflictError):
        env.manager.recover()
    env.manager.stop(run_id)
    gate.release.set()
    assert env.manager.wait(run_id, WAIT)


def test_pending_shows_the_open_approval(env: Env) -> None:
    run_id = env.start()
    pending: list[str | None] = []

    def answer(approval: Approval) -> Choice | None:
        found = env.manager.pending(run_id)
        pending.append(found.kind.value if found else None)
        return Choice(next(c.id for c in approval.choices if c.approves))

    env.drive(run_id, answer)
    assert pending == ["plan", "breaker"]
    assert env.manager.pending(run_id) is None


def test_approval_needed_is_answerable_as_soon_as_it_is_stored(env: Env) -> None:
    """The pending approval is registered before its event is stored, so the answer
    never races the registration (no 409 for a question the client can see)."""
    run_id = env.start()
    seen: list[ApprovalNeededEvent] = []

    def on_event(event: Event | UnknownEvent) -> None:
        if isinstance(event, ApprovalNeededEvent):
            seen.append(event)
            assert env.manager.pending(run_id) is not None

    env.drive(run_id, on_event=on_event)
    assert len(seen) == 2


def test_a_manager_needs_no_global_state(tmp_path: Path) -> None:
    one = make_env(tmp_path / "one")
    two = make_env(tmp_path / "two")
    a, b = one.start(), two.start()
    one.drive(a)
    two.drive(b)
    assert isinstance(one.manager, RunManager)
    assert one.store.get_run(a).status is two.store.get_run(b).status is RunStatus.FINISHED
