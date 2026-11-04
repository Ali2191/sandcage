"""Helpers for API tests: a run manager over the real SQLite store with scripted models
and the local runner, and a driver that answers approvals the way the panel does (by
reading the store's event stream, then calling ``approve``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sandcage.api.guard import DemoGuard
from sandcage.api.runs import ModelsFactory, RunManager, StartRequest
from sandcage.api.settings import DemoSettings, ProviderSettings
from sandcage.contract.approval import Approval, ApprovalKind
from sandcage.contract.events import ApprovalNeededEvent, DoneEvent, Event, UnknownEvent
from sandcage.events.store import EventStore
from sandcage.sandbox.run import RunnerFactory
from tests.agent_helpers import LocalRunner
from tests.harness_helpers import CLARIFY_ANSWER
from tests.policy_helpers import engine
from tests.sandbox_helpers import DEMO_REPO

TASK = "Set up the project following the README, then fix the failing test."
WAIT = 60.0


@dataclass(frozen=True)
class Choice:
    choice: str
    answer: str | None = None


Answerer = Callable[[Approval], Choice | None]  # None: leave it unanswered


def approve_all(approval: Approval) -> Choice:
    if approval.free_text is not None:
        return Choice("answer", CLARIFY_ANSWER)
    return Choice(next(c.id for c in approval.choices if c.approves))


def refuse(approval: Approval) -> Choice:
    return Choice(next(c.id for c in approval.choices if not c.approves))


def answers(by_kind: dict[ApprovalKind, Answerer], default: Answerer = approve_all) -> Answerer:
    return lambda approval: by_kind.get(approval.kind, default)(approval)


@dataclass
class Env:
    tmp: Path
    store: EventStore
    guard: DemoGuard
    manager: RunManager
    asked: list[Approval] = field(default_factory=list)

    def start(self, scenario: str = "demo", **options: object) -> str:
        request = StartRequest(
            repo="invoice-calc",
            task=TASK,
            fake_scenario=scenario,
            **options,  # type: ignore[arg-type]
        )
        return self.manager.start(request, client="127.0.0.1")

    def drive(
        self,
        run_id: str,
        answerer: Answerer = approve_all,
        *,
        on_event: Callable[[Event | UnknownEvent], None] | None = None,
    ) -> list[Event | UnknownEvent]:
        """Read the run's stream from the store until ``done``, answering approvals."""
        seen: list[Event | UnknownEvent] = []
        last = 0
        while True:
            batch = self.store.wait_for_events(run_id, after_id=last, timeout=WAIT)
            assert batch, f"no event for {WAIT} s; seen {[e.kind for e in seen]}"
            for event in batch:
                last = event.id
                seen.append(event)
                if on_event is not None:
                    on_event(event)
                if isinstance(event, ApprovalNeededEvent):
                    self.asked.append(event.approval)
                    chosen = answerer(event.approval)
                    if chosen is not None:
                        self.manager.approve(
                            run_id, event.approval.id, chosen.choice, chosen.answer
                        )
                if isinstance(event, DoneEvent):
                    assert self.manager.wait(run_id, WAIT)
                    return seen

    def kinds(self, run_id: str) -> list[str]:
        return [e.kind for e in self.store.events(run_id)]


def make_env(
    tmp: Path,
    *,
    demo: DemoSettings | None = None,
    provider: ProviderSettings | None = None,
    runner_factory: RunnerFactory = LocalRunner,
    models_factory: ModelsFactory | None = None,
    approval_timeout: float | None = None,
    stop_wait: float = 3.0,
    store: EventStore | None = None,
) -> Env:
    tmp.mkdir(parents=True, exist_ok=True)
    store = store or EventStore(tmp / "sandcage.db")
    settings = demo or DemoSettings(runs_per_client_per_hour=100, runs_per_day=100)
    guard = DemoGuard(settings, store)
    manager = RunManager(
        store=store,
        guard=guard,
        repos={"invoice-calc": DEMO_REPO},
        runner_factory=runner_factory,
        provider=provider or ProviderSettings(),
        learned_path=tmp / "data" / "learned.yaml",
        runtime_label=lambda: "local runner (tests)",
        models_factory=models_factory,
        engine_factory=engine,
        approval_timeout=approval_timeout,
        stop_wait=stop_wait,
        temp_root=tmp,
    )
    return Env(tmp, store, guard, manager)
