"""Background runs for the API: a few at a time, each on its own thread, every event into
the real event store, human approvals, stop, rewind, release and learn.

Ordering rules (Architecture/Concurrency.md, History/Bugs #2):

- **Admission** (slot count, the demo guard's limits, the run row) happens under one
  lock, so two simultaneous starts cannot both squeeze under a limit.
- **Approvals**: the pending approval is registered when its ``approval_needed`` event
  passes through the sink, *before* the event is stored, so a client that sees the event
  can always answer it. It is answered by approval id + choice id (+ free text) through
  :meth:`Approval.resolve`; no answer within the timeout resolves as the record says.
- **Stop**: the harness's flag is set (and one ``stopped`` event recorded) *before* any
  waiting question is answered; then the question gets its timeout answer marked
  ``by=stop``, then the sandbox is destroyed. A second stop records nothing.
- **Done**: when the harness emits ``done``, the run row is finished and the slot freed
  *before* the event is stored, so a client starting a new run the moment it sees
  ``done`` is never told the server is busy.
- **Recovery** at startup: runs left ``running`` by a dead process become
  ``interrupted`` with a final ``done`` event, so their streams end.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sandcage.agent.llm import ChatClient, OpenAIChatClient
from sandcage.agent.scenarios import ScenarioId, scripted_models
from sandcage.api.errors import ConflictError, InvalidError, NotFoundError, UnavailableError
from sandcage.api.guard import DemoGuard
from sandcage.api.settings import ProviderSettings
from sandcage.bench import repo as bench_repo
from sandcage.bench.catalogue import find as find_scenario
from sandcage.bench.runner import models_for
from sandcage.contract.approval import Approval, ApprovalAnswerError, Resolution
from sandcage.contract.common import Phase
from sandcage.contract.events import (
    ApprovalNeededEvent,
    ApprovalResolvedEvent,
    CheckpointEvent,
    CheckpointState,
    DoneEvent,
    Event,
    LearnEvent,
    LearnSuggestion,
    ResolvedBy,
    RunStatus,
    StoppedEvent,
)
from sandcage.events.store import EventStore, Outputs, RunRecord, StoreError
from sandcage.harness.checkpoints import CheckpointError, Checkpoints
from sandcage.harness.deliver import RELEASE_NOTE
from sandcage.harness.guards import Guards, Limits
from sandcage.harness.pipeline import Answer, Harness, Models, RunOptions
from sandcage.learn.learned import (
    LearnedConflictError,
    LearnedLayer,
    LearnedRule,
    LearnedRuleError,
    accept,
    load_rules,
)
from sandcage.masking import mask
from sandcage.policy import PolicyEngine
from sandcage.report.report import outcome
from sandcage.roles.reader import QuarantinedReader
from sandcage.sandbox.run import GateMode, RunnerFactory, SandboxRun

STOPPED_SUMMARY: Final = "You stopped this run. Nothing was delivered."
INTERRUPTED_KEPT: Final = (
    "The API restarted while this run was going. Its patch had already been made and can "
    "still be downloaded."
)
INTERRUPTED_NOTHING: Final = (
    "The API restarted while this run was going, so it was cut off. Nothing was delivered."
)


class BusyError(ConflictError):
    def __init__(self, running: Sequence[str]) -> None:
        super().__init__(
            "The demo is busy: the most runs allowed at once are in progress "
            f"({', '.join(running)}). Try again when one finishes."
        )
        self.running = tuple(running)


@dataclass(frozen=True)
class StartRequest:
    repo: str
    task: str
    guard: bool = True
    approve_plan: bool = False
    done_criteria: tuple[str, ...] = ()
    fake_scenario: str | None = None
    scenario: str | None = None  # a Bench scenario id: its repo is built for this run only


ModelsFactory = Callable[[StartRequest], Models]


# --- one active run --------------------------------------------------------------------------


@dataclass
class _Pending:
    approval: Approval
    answered: threading.Event = field(default_factory=threading.Event)
    answer: Answer | None = None
    closed: bool = False


@dataclass
class _Active:
    id: str
    repo: str
    guard: bool
    provider: str  # this run's own labels: "scripted" and no model for a scripted run
    model: str | None
    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None
    harness: Harness | None = None
    sandbox: SandboxRun | None = None
    pending: _Pending | None = None
    stop_requested: bool = False
    stop_recorded: bool = False
    done_sent: bool = False
    finished: threading.Event = field(default_factory=threading.Event)
    close_lock: threading.Lock = field(default_factory=threading.Lock)
    scratch: Path | None = None  # a Bench scenario's temp repo, removed when the run ends

    def stopping(self) -> bool:
        """A method, so every check really reads the flag another thread may set."""
        return self.stop_requested

    def close_sandbox(self) -> None:
        """Destroy the container and workspace. Safe from any thread and repeatable: the
        stop endpoint closes it at once, and the run thread closes it again on its way
        out (which also cleans up after an ``open`` that raced the stop)."""
        with self.lock:
            sandbox = self.sandbox
        if sandbox is None:
            return
        with self.close_lock:
            sandbox.close()


# --- the manager ---------------------------------------------------------------------------


class RunManager:
    def __init__(
        self,
        *,
        store: EventStore,
        guard: DemoGuard,
        repos: Mapping[str, Path],
        runner_factory: RunnerFactory,
        provider: ProviderSettings,
        learned_path: Path,
        runtime_label: Callable[[], str | None] = lambda: None,
        models_factory: ModelsFactory | None = None,
        engine_factory: Callable[[], PolicyEngine] = PolicyEngine.from_file,
        limits: Callable[[], Limits] = Limits.from_env,
        approval_timeout: float | None = None,
        stop_wait: float = 3.0,
        temp_root: Path | None = None,
        clock: Callable[[], float] = time.time,
        new_id: Callable[[], str] = lambda: secrets.token_hex(6),
    ) -> None:
        self._store = store
        self._guard = guard
        self._repos = dict(repos)
        self._runner_factory = runner_factory
        self._provider = provider
        self._learned_path = learned_path
        self._runtime_label = runtime_label
        self._models_factory = models_factory or self._default_models
        self._engine_factory = engine_factory
        self._limits = limits
        self._approval_timeout = approval_timeout
        self._stop_wait = stop_wait
        self._temp_root = temp_root
        self._clock = clock
        self._new_id = new_id
        self._lock = threading.Lock()
        self._learn_lock = threading.Lock()
        self._active: dict[str, _Active] = {}

    # --- reading ---------------------------------------------------------------------------

    @property
    def repos(self) -> list[str]:
        return sorted(self._repos)

    def active_ids(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def run(self, run_id: str) -> RunRecord:
        try:
            return self._store.get_run(run_id)
        except StoreError:
            raise NotFoundError("There is no such run.") from None

    def pending(self, run_id: str) -> Approval | None:
        active = self._find(run_id)
        if active is None:
            return None
        with active.lock:
            found = active.pending
            return found.approval if found is not None and not found.closed else None

    def acting(self, run_id: str) -> bool:
        active = self._find(run_id)
        harness = active.harness if active else None
        return bool(harness and harness.acting)

    def checkpoints(self, run_id: str) -> list[dict[str, object]]:
        """Checkpoints of the run: live while it runs, from its stored events after."""
        self.run(run_id)
        active = self._find(run_id)
        harness = active.harness if active else None
        if harness is not None:
            return [
                {"id": info.id, "step": info.step, "files": list(info.files)}
                for info in harness.ctx.checkpoints.infos()
            ]
        found: dict[int, dict[str, object]] = {0: {"id": 0, "step": None, "files": []}}
        for event in self._store.events(run_id):
            if isinstance(event, CheckpointEvent) and event.state is CheckpointState.SAVED:
                found[event.checkpoint] = {
                    "id": event.checkpoint,
                    "step": event.step,
                    "files": list(event.files),
                }
        return [found[key] for key in sorted(found)]

    def wait(self, run_id: str, timeout: float) -> bool:
        """Wait until the run's thread has fully ended (tests and shutdown)."""
        active = self._find(run_id)
        return True if active is None else active.finished.wait(timeout)

    # --- starting --------------------------------------------------------------------------

    def start(self, request: StartRequest, *, client: str) -> str:
        """Validate, then admit atomically (slot, demo limits, run row) and start the
        thread. Raises an :class:`ApiError` subclass the HTTP layer maps to a status."""
        path = self._repos.get(request.repo)
        if request.scenario is not None:
            if find_scenario(request.scenario) is None:
                raise NotFoundError("There is no Bench scenario by that name.")
            if request.fake_scenario is not None:
                raise InvalidError(
                    "A Bench scenario brings its own script; fake_scenario is not allowed."
                )
            path = None
        elif path is None:
            raise NotFoundError("There is no demo repo by that name.")
        if request.fake_scenario is not None and request.fake_scenario not in {
            s.value for s in ScenarioId
        }:
            raise InvalidError("That fake scenario does not exist.")
        problem = self._provider.problem
        if problem is not None:
            raise UnavailableError(problem)
        with self._lock:
            if len(self._active) >= self._guard.settings.max_concurrent:
                raise BusyError(list(self._active))
            run_id = self._new_id()
            provider, model = self._labels(request)
            cap = self._guard.admit(client, run_id)
            try:
                self._store.create_run(
                    run_id,
                    repo=request.repo,
                    task=request.task,
                    guard=request.guard,
                    provider=provider,
                    model=model,
                )
            except BaseException:
                self._guard.finish(run_id)
                raise
            active = _Active(run_id, request.repo, request.guard, provider, model)
            thread = threading.Thread(
                target=self._run,
                args=(active, request, path, cap),
                name=f"run-{run_id}",
                daemon=True,
            )
            active.thread = thread
            self._active[run_id] = active
            thread.start()
        return run_id

    def _scripted(self, request: StartRequest) -> bool:
        """A run is scripted when the server's provider is, or when the request names a
        fake scenario: its script is the agent and no model is ever called, whatever
        provider the server is configured with."""
        return self._provider.scripted or request.fake_scenario is not None

    def _labels(self, request: StartRequest) -> tuple[str, str | None]:
        """The provider and model recorded for a run. A scripted run never carries the
        configured provider's model name: it must not read as a real model's behaviour."""
        if self._provider.scripted:
            return self._provider.provider, None
        if self._scripted(request):
            return "scripted", None
        return self._provider.provider, self._provider.model

    def _default_models(self, request: StartRequest) -> Models:
        if self._scripted(request):
            chosen = find_scenario(request.scenario) if request.scenario else None
            if chosen is not None:
                return models_for(chosen)
            scripted = scripted_models(request.fake_scenario or ScenarioId.DEMO)
            return Models(
                understand=scripted.understand,
                scout=scripted.scout,
                planner=scripted.planner,
                executor=scripted.executor,
                verifier=scripted.verifier,
                redteam=scripted.redteam,
                reader=scripted.reader,
            )
        key, model = self._provider.api_key, self._provider.model
        if key is None or model is None:  # start() checked; kept for the type checker
            raise UnavailableError("The model provider is not configured.")
        client: ChatClient = OpenAIChatClient(
            base_url=self._provider.base_url,
            api_key=key,
            model=model,
            retries=self._provider.retries,
        )
        return Models(client, client, client, client, client, client, client)

    # --- the run thread --------------------------------------------------------------------

    def _run(self, active: _Active, request: StartRequest, path: Path | None, cap: int) -> None:
        run_id = active.id
        try:
            self._execute(active, request, path, cap)
        except Exception as problem:  # noqa: BLE001 - any failure ends the run as error
            message = mask(f"{type(problem).__name__}: {problem}")[:500]
            status = RunStatus.STOPPED if active.stop_requested else RunStatus.ERROR
            summary = STOPPED_SUMMARY if active.stop_requested else f"The run failed: {message}"
            self._final(active, status, summary)
        finally:
            try:
                active.close_sandbox()
            finally:
                if active.scratch is not None:
                    bench_repo.remove(active.scratch)
                if not active.done_sent:  # the harness never reached done
                    status = RunStatus.STOPPED if active.stop_requested else RunStatus.ERROR
                    summary = STOPPED_SUMMARY if active.stop_requested else "The run failed."
                    self._final(active, status, summary)
                self._free(run_id)
                active.finished.set()

    def _execute(self, active: _Active, request: StartRequest, path: Path | None, cap: int) -> None:
        run_id = active.id
        if path is None:  # a Bench scenario: build its repo for this run
            chosen = find_scenario(request.scenario or "")
            if chosen is None:  # start() checked; kept for the type checker
                raise NotFoundError("There is no Bench scenario by that name.")
            path = bench_repo.materialize(chosen, self._temp_root)
            active.scratch = path
        engine = self._engine_factory()
        reserved = [spec.rule_id for spec in engine.rulebook.all_rules]
        layer = LearnedLayer(load_rules(self._learned_path, reserved=reserved), request.repo)
        models = self._models_factory(request)
        limits = self._limits().capped(cap)
        guards = Guards(run_id, limits, wall=self._clock)
        guards.meter.listeners.append(lambda tokens: self._spent(run_id, tokens))
        checkpoints = Checkpoints(path, run_id=run_id)
        reader = QuarantinedReader(
            guards.metered(models.reader, "reader"),
            budget_left=lambda: guards.meter.total < guards.limits.max_tokens,
        )
        sandbox = SandboxRun(
            path,
            engine=engine,
            runner_factory=self._runner_factory,
            mode=GateMode.ENFORCE if request.guard else GateMode.OBSERVE,
            layers=(layer,),
            on_write=checkpoints.on_write,
            tools={"rollback": checkpoints.rollback},
            reader=reader,
            temp_root=self._temp_root,
        )
        with active.lock:
            if active.stopping():
                return  # stopped before anything was built; _run's finally ends it
            active.sandbox = sandbox
        sandbox.open()
        harness = Harness(
            run_id=run_id,
            models=models,
            run=sandbox,
            original=path,
            engine=engine,
            options=RunOptions(
                task=request.task,
                repo=request.repo,
                done_criteria=request.done_criteria,
                guard=request.guard,
                approve_plan=request.approve_plan,
                limits=limits,
                model=active.model,
                provider=active.provider,
                sandbox_runtime=self._runtime_label(),
                learned_suspicious=layer.suspicious,
                learned_ids=layer.ids,
            ),
            checkpoints=checkpoints,
            reader=reader,
            human=lambda approval: self._wait_for_human(active, approval),
            sink=lambda event: self._sink(active, event),
            save_outputs=lambda outputs: self._save(run_id, outputs),
            clock=self._clock,
            guards=guards,
        )
        with active.lock:
            if active.stopping():
                return  # stop() saw no harness and recorded the stop itself
            active.harness = harness
        harness.execute()

    def _spent(self, run_id: str, tokens: int) -> None:
        self._store.add_tokens(tokens)
        self._guard.spend(run_id, tokens)

    def _save(self, run_id: str, outputs: Outputs) -> None:
        self._store.save_outputs(run_id, outputs)

    def _sink(self, active: _Active, event: Event) -> None:
        if isinstance(event, ApprovalNeededEvent):
            with active.lock:  # answerable before anyone can see the question
                active.pending = _Pending(event.approval)
        elif isinstance(event, ApprovalResolvedEvent):
            with active.lock:
                found = active.pending
                if found is not None and found.approval.id == event.approval_id:
                    found.closed = True
                    active.pending = None
        elif isinstance(event, DoneEvent):
            self._finish(active, event)
            return
        self._store.append(event)

    def _finish(self, active: _Active, done: DoneEvent) -> None:
        """Finish the row and free the slot, then announce ``done`` (in that order)."""
        tokens = done.budget.tokens if done.budget is not None else 0
        self._store.finish_run(active.id, done, tokens=tokens)
        self._free(active.id)
        active.done_sent = True
        self._store.append(done)

    def _final(self, active: _Active, status: RunStatus, summary: str) -> None:
        """A ``done`` for a run the harness could not finish (stopped early or failed)."""
        if active.done_sent:
            return
        done = DoneEvent(
            run_id=active.id,
            phase=Phase.REPORT,
            ts=self._clock(),
            status=status,
            summary=summary,
            steps=0,
            guard=active.guard,
            outcome=outcome(status, withheld=False),
            provider=active.provider,
            model=active.model,
            sandbox_runtime=self._runtime_label(),
        )
        self._finish(active, done)

    def _free(self, run_id: str) -> None:
        with self._lock:
            self._active.pop(run_id, None)
        self._guard.finish(run_id)

    def _wait_for_human(self, active: _Active, approval: Approval) -> Answer:
        with active.lock:
            pending = active.pending
        if pending is None or pending.approval.id != approval.id:
            return Answer(approval.timeout_resolution(), ResolvedBy.ERROR)
        timeout = (
            self._approval_timeout
            if self._approval_timeout is not None
            else float(approval.timeout_seconds)
        )
        pending.answered.wait(timeout)
        with active.lock:
            pending.closed = True
            if active.pending is pending:
                active.pending = None
            answer = pending.answer
        return answer or Answer(approval.timeout_resolution(), ResolvedBy.TIMEOUT)

    def _find(self, run_id: str) -> _Active | None:
        with self._lock:
            return self._active.get(run_id)

    # --- control ---------------------------------------------------------------------------

    def approve(
        self, run_id: str, approval_id: str, choice: str, answer: str | None = None
    ) -> Resolution:
        self.run(run_id)
        active = self._find(run_id)
        if active is None:
            raise ConflictError("This run is not waiting for an answer.")
        with active.lock:
            pending = active.pending
            if (
                pending is None
                or pending.closed
                or pending.answer is not None
                or pending.approval.id != approval_id
                or active.stop_requested
            ):
                raise ConflictError("Nothing is waiting for that answer.")
            try:
                resolution = pending.approval.resolve(choice, answer)
            except ApprovalAnswerError as problem:
                raise InvalidError(str(problem)) from None
            pending.answer = Answer(resolution, ResolvedBy.HUMAN)
            pending.answered.set()
        return resolution

    def stop(self, run_id: str) -> RunStatus | str:
        """Stop in any state. Returns ``stopped``, ``stopping`` (still unwinding, e.g.
        inside a slow model call) or the final status of a run that had already ended."""
        record = self.run(run_id)
        active = self._find(run_id)
        if active is None:
            return record.status
        with active.lock:
            first = not active.stop_requested
            active.stop_requested = True
            harness = active.harness
        if first:
            # 1. the flag and one stopped event, before any waiting question is answered
            if harness is not None:
                harness.request_stop("human")
            else:
                self._record_stop(active)
            # 2. then the waiting question (if any) gets its no, marked as a stop
            with active.lock:
                pending = active.pending
                if pending is not None and pending.answer is None and not pending.closed:
                    resolution = pending.approval.timeout_resolution()
                    pending.answer = Answer(resolution, ResolvedBy.STOP)
                    pending.answered.set()
            # 3. then the sandbox goes, now
            active.close_sandbox()
        if active.finished.wait(self._stop_wait):
            return self.run(run_id).status
        return "stopping"

    def _record_stop(self, active: _Active) -> None:
        with active.lock:
            if active.stop_recorded:
                return
            active.stop_recorded = True
        self._store.append(
            StoppedEvent(
                run_id=active.id,
                phase=Phase.ACT,
                ts=self._clock(),
                by="human",
                summary=STOPPED_SUMMARY,
                guard=active.guard,
            )
        )

    def rewind(self, run_id: str, checkpoint: int) -> None:
        self.run(run_id)
        active = self._find(run_id)
        harness = active.harness if active else None
        if harness is None or not harness.acting:
            raise ConflictError("A rewind works only while the agent is acting.")
        try:
            harness.request_rewind(checkpoint)
        except CheckpointError as problem:
            raise InvalidError(str(problem)) from None

    def release(self, run_id: str) -> None:
        self.run(run_id)
        if not self._store.release(run_id, RELEASE_NOTE):
            raise ConflictError("There is no withheld patch to release.")

    def learn(self, run_id: str) -> LearnEvent | None:
        self.run(run_id)
        found = [e for e in self._store.events(run_id) if isinstance(e, LearnEvent)]
        return found[-1] if found else None

    def accept(self, run_id: str, suggestion_id: str) -> LearnedRule:
        record = self.run(run_id)
        learned = self.learn(run_id)
        chosen: LearnSuggestion | None = None
        for suggestion in learned.suggestions if learned else []:
            if suggestion.id == suggestion_id:
                chosen = suggestion
        if chosen is None:
            raise NotFoundError("That run has no such suggestion.")
        reserved = [spec.rule_id for spec in self._engine_factory().rulebook.all_rules]
        with self._learn_lock:
            try:
                return accept(self._learned_path, chosen, record.repo, reserved=reserved)
            except LearnedConflictError as problem:
                raise ConflictError(str(problem)) from None
            except LearnedRuleError as problem:
                raise InvalidError(str(problem)) from None

    # --- startup ---------------------------------------------------------------------------

    def recover(self) -> list[str]:
        """Close every run a previous process left ``running`` as ``interrupted`` with a
        final ``done`` event (so its stream ends). Only before any run starts here."""
        with self._lock:
            if self._active:
                raise ConflictError("Recovery runs only before any run starts.")
        recovered: list[str] = []
        for run_id in self._store.running_runs():
            record = self._store.get_run(run_id)
            outputs = self._store.outputs(run_id)
            kept = outputs is not None
            done = DoneEvent(
                run_id=run_id,
                phase=Phase.REPORT,
                ts=self._clock(),
                status=RunStatus.INTERRUPTED,
                summary=INTERRUPTED_KEPT if kept else INTERRUPTED_NOTHING,
                steps=sum(1 for e in self._store.events(run_id) if e.kind == "step"),
                guard=record.guard,
                outcome=outcome(RunStatus.INTERRUPTED, withheld=bool(outputs and outputs.blocked)),
                provider=record.provider,
                model=record.model,
                delivered=bool(outputs and not outputs.blocked),
                delivery_blocked=bool(outputs and outputs.blocked),
            )
            self._store.finish_run(run_id, done, tokens=record.tokens)
            self._store.append(done)
            recovered.append(run_id)
        return recovered
