"""The harness: one run, end to end, as small stage objects.

Every stage has the same interface: ``run(ctx) -> RunStatus | None``. It reads what earlier
stages left on the :class:`RunContext`, emits its events through ``ctx.emit`` and returns
a final status to end the run early (``None`` = carry on). The order is the run
walkthrough's:

    Understand → Scout → Plan → Approve → Lock → Act → Verify      (until one ends the run)
    Red-team → Deliver → Report → Learn                              (every run not stopped)
    done                                                             (always last)

The harness owns the hooks. The stop flag is a ``threading.Event`` (in :class:`Guards`)
checked at every gate and by every role's metered client, so no model is called after a
stop. **Stop ordering** (History/Bugs #2): :meth:`Harness.request_stop` sets the flag and
records one ``stopped`` event *before* any waiting question is answered; every answer is
read only after re-checking the flag, and the flag is checked again before delivery. A
stopped run delivers, reviews, reports and learns nothing.

Checkpoint events come after the step's own event. Human waiting time never counts
against the time budget. Every event carries the budget snapshot.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from sandcage.agent.llm import ChatClient
from sandcage.agent.loop import Agent, AgentResult, AmendOutcome, Hooks
from sandcage.agent.protocol import AgentAction
from sandcage.contract.approval import (
    ActionContext,
    Approval,
    Resolution,
    amendment_approval,
    plan_approval,
    question_approval,
)
from sandcage.contract.common import MatchedRule, Phase, Plan
from sandcage.contract.events import (
    ApprovalNeededEvent,
    ApprovalResolvedEvent,
    DecisionCounts,
    DoneEvent,
    Event,
    PlanEvent,
    PlanState,
    ResolvedBy,
    RunStatus,
    ScoutEvent,
    ScoutFlag,
    StepEvent,
    StoppedEvent,
    UnderstandEvent,
    UnderstandState,
)
from sandcage.contract.wording import secret_check
from sandcage.events.store import Outputs
from sandcage.harness.checkpoints import Checkpoints
from sandcage.harness.deliver import Delivery, build_patch, summary_markdown
from sandcage.harness.guards import Guards, Limits
from sandcage.masking import mask
from sandcage.plan.layer import AMENDMENT, PlanLayer, propose_amendment
from sandcage.plan.planner import PlanError, file_tree, make_plan
from sandcage.policy import Decision, PolicyEngine, RuleSource
from sandcage.report.report import Report, ReportInput, build_report, outcome
from sandcage.roles.reader import QuarantinedReader
from sandcage.roles.redteam import Review, review
from sandcage.roles.scout import ScoutMap, scout
from sandcage.roles.understand import Understanding, effective_criteria, understand
from sandcage.roles.verify import Evidence, Verdict, choose_test_command, feedback_message, verify
from sandcage.sandbox.diff import WorkspaceDiff, workspace_diff
from sandcage.sandbox.run import GateMode, SandboxRun

MAX_VERIFY_RETRIES: Final = 2
INJECTION_TAG: Final = "prompt_injection_suspected"


# --- inputs ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Models:
    """One chat client per role (scripted in tests, Vultr in production)."""

    understand: ChatClient
    scout: ChatClient
    planner: ChatClient
    executor: ChatClient
    verifier: ChatClient
    redteam: ChatClient
    reader: ChatClient


@dataclass(frozen=True)
class Answer:
    """A human's answer to an approval, and how it came (a click, a timeout, an error)."""

    resolution: Resolution
    by: ResolvedBy = ResolvedBy.HUMAN


Human = Callable[[Approval], Answer]


def human(answer: Callable[[Approval], Resolution]) -> Human:
    """Wrap a plain answering function as a :data:`Human`."""
    return lambda approval: Answer(answer(approval), ResolvedBy.HUMAN)


def nobody(approval: Approval) -> Answer:
    """No human is there: every approval times out (always a no)."""
    return Answer(approval.timeout_resolution(), ResolvedBy.TIMEOUT)


@dataclass(frozen=True)
class RunOptions:
    task: str
    repo: str = "invoice-calc"
    done_criteria: Sequence[str] = ()
    guard: bool = True
    approve_plan: bool = False  # force a plan approval even for a low-risk plan
    limits: Limits = field(default_factory=Limits)
    model: str | None = None
    provider: str | None = None
    sandbox_runtime: str | None = None
    learned_suspicious: Sequence[str] = ()


@dataclass
class HarnessResult:
    status: RunStatus
    summary: str
    events: list[Event]
    outputs: Outputs | None
    report: Report | None
    delivery: Delivery | None
    plan: Plan | None
    verdicts: list[Verdict]


# --- the shared state -------------------------------------------------------------------------


@dataclass
class RunContext:
    run_id: str
    models: Models
    run: SandboxRun
    original: Path
    engine: PolicyEngine
    options: RunOptions
    guards: Guards
    checkpoints: Checkpoints
    sink: Callable[[Event], None]
    human: Human
    clock: Callable[[], float] = time.time
    events: list[Event] = field(default_factory=list)
    understanding: Understanding | None = None
    scout_map: ScoutMap | None = None
    test_command: str | None = None
    plan: Plan | None = None
    plan_asked: bool = False
    layer: PlanLayer | None = None
    agent: Agent | None = None
    agent_result: AgentResult | None = None
    verdicts: list[Verdict] = field(default_factory=list)
    retries: int = 0
    diff: WorkspaceDiff | None = None
    review: Review | None = None
    delivery: Delivery | None = None
    report: Report | None = None
    rollbacks: int = 0
    run_reader: QuarantinedReader | None = None
    error: str | None = None
    _emit_lock: threading.Lock = field(default_factory=threading.Lock)
    _rewind_lock: threading.Lock = field(default_factory=threading.Lock)
    _rewind: int | None = None

    # --- events ----------------------------------------------------------------------------

    def is_stopped(self) -> bool:
        """A method, not a property: another thread can set the flag at any moment, so
        every check must really read it."""
        return self.guards.stopped

    def emit(self, event: Event) -> Event:
        if event.budget is None:
            event = event.model_copy(update={"budget": self.guards.budget()})
        with self._emit_lock:
            self.events.append(event)
            self.sink(event)
        return event

    def on_step(self, event: StepEvent) -> None:
        """The agent's step event, then the checkpoint events it caused (causal order)."""
        self.emit(event)
        for checkpoint in self.checkpoints.drain_events():
            if checkpoint.state.value == "restored":
                self.rollbacks += 1
            self.emit(checkpoint.model_copy(update={"step": event.step}))

    @property
    def steps(self) -> list[StepEvent]:
        return [event for event in self.events if isinstance(event, StepEvent)]

    @property
    def brief(self) -> str:
        return self.understanding.brief() if self.understanding else mask(self.options.task)

    def budget_left(self) -> bool:
        return self.guards.meter.total < self.guards.limits.max_tokens and not self.is_stopped()

    # --- asking a human ----------------------------------------------------------------------

    def ask(self, approval: Approval) -> Resolution:
        """Announce, wait (not counted as run time), record the answer. After a stop, the
        answer is always a no, whatever the human clicked."""
        self.emit(
            ApprovalNeededEvent(
                run_id=self.run_id, phase=approval.phase, ts=self.clock(), approval=approval
            )
        )
        if self.is_stopped():
            answer = Answer(approval.timeout_resolution(), ResolvedBy.STOP)
        else:
            try:
                with self.guards.clock.waiting():
                    answer = self.human(approval)
            except Exception:  # noqa: BLE001 - spec: an approver failure counts as no
                answer = Answer(approval.timeout_resolution(), ResolvedBy.ERROR)
        if self.is_stopped():  # the flag is set before a waiting question is answered
            answer = Answer(approval.timeout_resolution(), ResolvedBy.STOP)
        resolution = answer.resolution
        self.emit(
            ApprovalResolvedEvent(
                run_id=self.run_id,
                phase=approval.phase,
                ts=self.clock(),
                approval_id=approval.id,
                approval_kind=approval.kind,
                step=approval.step,
                choice=resolution.choice,
                approved=resolution.approved,
                by=answer.by,
                answer=resolution.answer,
            )
        )
        return resolution

    # --- human rewind ------------------------------------------------------------------------

    def request_rewind(self, checkpoint: int) -> None:
        self.checkpoints.get(checkpoint)  # raises CheckpointError for an unknown id
        with self._rewind_lock:
            self._rewind = checkpoint

    def apply_rewind(self) -> None:
        with self._rewind_lock:
            checkpoint, self._rewind = self._rewind, None
        if checkpoint is None:
            return
        result = self.checkpoints.rewind(checkpoint, by="human")
        for event in self.checkpoints.drain_events():
            if event.state.value == "restored":
                self.rollbacks += 1
            self.emit(event)
        if self.agent is not None and isinstance(result, dict) and "error" not in result:
            self.agent.note(f"A human rewound the workspace to checkpoint {checkpoint}.")
            self.guards.progress()


# --- the stages -------------------------------------------------------------------------------


class Stage(Protocol):
    phase: Phase

    def run(self, ctx: RunContext) -> RunStatus | None: ...


class UnderstandStage:
    phase = Phase.UNDERSTAND

    def run(self, ctx: RunContext) -> RunStatus | None:
        client = ctx.guards.metered(ctx.models.understand, "understand")
        found = understand(client, ctx.options.task)
        ctx.understanding = found
        state = UnderstandState.ASKING if found.question else UnderstandState.UNDERSTOOD
        ctx.emit(self._event(ctx, found, state))
        if not found.question:
            return None
        resolution = ctx.ask(question_approval(found.question))
        if ctx.is_stopped():
            return RunStatus.STOPPED
        try:
            if not resolution.approved:
                raise ValueError("the question was not answered")
            ctx.understanding = found.answered(resolution.answer)
        except ValueError:
            ctx.emit(self._event(ctx, found, UnderstandState.UNANSWERED))
            return RunStatus.NEEDS_CLARIFICATION
        ctx.emit(self._event(ctx, ctx.understanding, UnderstandState.ANSWERED))
        return None

    @staticmethod
    def _event(ctx: RunContext, found: Understanding, state: UnderstandState) -> UnderstandEvent:
        return UnderstandEvent(
            run_id=ctx.run_id,
            phase=Phase.UNDERSTAND,
            ts=ctx.clock(),
            state=state,
            task=found.task,
            done_criteria=list(found.done_criteria),
            question=found.question,
            answer=found.answer,
            valid=found.valid,
        )


class ScoutStage:
    phase = Phase.SCOUT

    def run(self, ctx: RunContext) -> RunStatus | None:
        found = scout(
            ctx.guards.metered(ctx.models.scout, "scout"),
            ctx.run.workspace.path,
            is_secret=secret_check(ctx.engine.rulebook.context),
            learned=ctx.options.learned_suspicious,
            budget_left=ctx.budget_left,
        )
        ctx.scout_map = found
        for path in found.flagged:
            ctx.run.session.mark_suspicious(path)
        chosen = choose_test_command(found.hints())
        ctx.test_command = chosen.command
        ctx.emit(
            ScoutEvent(
                run_id=ctx.run_id,
                phase=Phase.SCOUT,
                ts=ctx.clock(),
                flags=[ScoutFlag(path=p, reason=r) for p, r in sorted(found.flags.items())],
                files_mapped=len(found.entries),
                model_calls=found.calls,
                skipped=sorted(found.skipped),
                test_command=chosen.command,
                test_command_source=chosen.source,
                ignored_hints=list(chosen.ignored),
            )
        )
        return RunStatus.STOPPED if ctx.is_stopped() else None


class PlanStage:
    phase = Phase.PLAN

    def run(self, ctx: RunContext) -> RunStatus | None:
        understood = ctx.understanding or Understanding(ctx.brief)
        found = ctx.scout_map or ScoutMap()
        try:
            plan = make_plan(
                ctx.guards.metered(ctx.models.planner, "planner"),
                ctx.engine,
                task=ctx.brief,
                done_criteria=effective_criteria(ctx.options.done_criteria, understood),
                files=file_tree(ctx.run.workspace.path),
                scout_map=found.for_planner(),
            )
        except PlanError as problem:
            if ctx.is_stopped():
                return RunStatus.STOPPED
            ctx.emit(_plan_event(ctx, PlanState.INVALID, None, error=mask(str(problem))))
            return RunStatus.PLAN_INVALID
        ctx.plan = plan
        ctx.plan_asked = ctx.options.approve_plan or plan.risk.value in {"high", "critical"}
        ctx.emit(_plan_event(ctx, PlanState.PROPOSED, plan, approval_required=ctx.plan_asked))
        return RunStatus.STOPPED if ctx.is_stopped() else None


class ApproveStage:
    phase = Phase.APPROVE

    def run(self, ctx: RunContext) -> RunStatus | None:
        if ctx.plan is None or not ctx.plan_asked:
            return None
        resolution = ctx.ask(plan_approval(ctx.plan))
        if ctx.is_stopped():
            return RunStatus.STOPPED
        if not resolution.approved:
            ctx.emit(_plan_event(ctx, PlanState.REJECTED, ctx.plan))
            return RunStatus.PLAN_REJECTED
        ctx.emit(_plan_event(ctx, PlanState.APPROVED, ctx.plan, by_human=True))
        return None


class LockStage:
    phase = Phase.PLAN

    def run(self, ctx: RunContext) -> RunStatus | None:
        if ctx.plan is None:
            return RunStatus.PLAN_INVALID
        ctx.layer = PlanLayer(ctx.plan.manifest, ctx.engine.rulebook.context)
        ctx.run.add_layer(ctx.layer)
        ctx.emit(_plan_event(ctx, PlanState.LOCKED, ctx.plan, by_human=ctx.plan_asked))
        return None


class ActStage:
    phase = Phase.ACT

    def run(self, ctx: RunContext) -> RunStatus | None:
        guards = ctx.guards
        guards.approver = ctx.ask

        def gate(action: AgentAction | None) -> RunStatus | None:
            if action is None and not guards.stopped:
                ctx.apply_rewind()
            return guards.gate(action)

        agent = Agent(
            client=guards.metered(ctx.models.executor, "executor"),
            executor=ctx.run,
            run_id=ctx.run_id,
            is_secret=secret_check(ctx.engine.rulebook.context),
            guard=ctx.options.guard,
            max_steps=guards.limits.max_steps,
            approver=ctx.ask,
            emit=ctx.on_step,
            hooks=Hooks(
                gate=gate,
                after_step=guards.after_step,
                annotate=guards.annotate,
                amender=lambda action, step: _amend(ctx, action, step),
            ),
            clock=ctx.clock,
        )
        ctx.agent = agent
        ctx.agent_result = agent.run_task(ctx.brief, ctx.plan)
        status = ctx.agent_result.status
        if ctx.is_stopped():
            return RunStatus.STOPPED
        return None if status is RunStatus.FINISHED else status


class VerifyStage:
    phase = Phase.VERIFY

    def run(self, ctx: RunContext) -> RunStatus | None:
        agent = ctx.agent
        if agent is None:
            return RunStatus.ERROR
        for attempt in range(1, MAX_VERIFY_RETRIES + 2):
            stopped = ctx.guards.gate(None)  # each verification costs tokens and time
            if stopped is not None:
                return stopped
            verdict = verify(
                _evidence(ctx),
                run=ctx.run.run_harness_command,
                judge=ctx.guards.metered(ctx.models.verifier, "verifier"),
                command=ctx.test_command,
            )
            ctx.verdicts.append(verdict)
            ctx.emit(verdict.event(ctx.run_id, attempt, ctx.clock()))
            if ctx.is_stopped():
                return RunStatus.STOPPED
            if verdict.passed:
                return RunStatus.FINISHED
            if attempt > MAX_VERIFY_RETRIES:
                return RunStatus.VERIFY_FAILED
            ctx.retries += 1
            ctx.guards.progress()
            feedback = feedback_message(
                verdict, MAX_VERIFY_RETRIES - attempt, ctx.checkpoints.summary()
            )
            result = agent.continue_task(feedback)
            ctx.agent_result = result
            if ctx.is_stopped():
                return RunStatus.STOPPED
            if result.status is not RunStatus.FINISHED:
                return result.status
        return RunStatus.VERIFY_FAILED  # pragma: no cover - the loop always returns


class RedteamStage:
    phase = Phase.REDTEAM

    def run(self, ctx: RunContext) -> RunStatus | None:
        workspace = ctx.run.workspace
        ctx.diff = workspace_diff(
            ctx.original,
            workspace.path,
            is_secret=secret_check(ctx.engine.rulebook.context),
            left_out=workspace.left_out,
        )
        found = review(
            ctx.guards.metered(ctx.models.redteam, "redteam"),
            ctx.brief,
            ctx.diff,
            budget_left=ctx.budget_left,
        )
        if ctx.is_stopped():
            return RunStatus.STOPPED
        ctx.review = found
        ctx.emit(found.event(ctx.run_id, ctx.clock()))
        patch = build_patch(ctx.original, workspace.path, ctx.diff)
        delivery = Delivery(patch, withheld=found.blocked)
        if found.blocked:
            resolution = ctx.ask(found.approval(len(ctx.steps)))
            if ctx.is_stopped():
                return RunStatus.STOPPED
            delivery.released_by_human = resolution.approved
        ctx.delivery = delivery
        return None


class DeliverStage:
    phase = Phase.DELIVER

    def run(self, ctx: RunContext) -> RunStatus | None:
        if ctx.is_stopped() or ctx.delivery is None:
            return RunStatus.STOPPED if ctx.is_stopped() else None
        ctx.emit(ctx.delivery.event(ctx.run_id, ctx.clock()))
        return None


class ReportStage:
    phase = Phase.REPORT

    def __init__(self, status: RunStatus) -> None:
        self.status = status

    def run(self, ctx: RunContext) -> RunStatus | None:
        source = report_input(ctx, self.status)
        ctx.report = build_report(source, ctx.clock())
        ctx.emit(ctx.report.event)
        if ctx.delivery is not None:
            final = ctx.verdicts[-1] if ctx.verdicts else None
            ctx.delivery.summary_md = summary_markdown(
                source,
                done_criteria=ctx.plan.done_criteria if ctx.plan else (),
                checks=final.checks if final else (),
                agent_summary=ctx.agent_result.summary if ctx.agent_result else "",
            )
        return None


# --- helpers for the stages -------------------------------------------------------------------


def _plan_event(
    ctx: RunContext,
    state: PlanState,
    plan: Plan | None,
    *,
    approval_required: bool = False,
    by_human: bool = False,
    error: str | None = None,
) -> PlanEvent:
    found = ctx.scout_map
    return PlanEvent(
        run_id=ctx.run_id,
        phase=Phase.APPROVE if state in {PlanState.APPROVED, PlanState.REJECTED} else Phase.PLAN,
        ts=ctx.clock(),
        state=state,
        plan=plan,
        scout_flags=found.flagged if found else [],
        approval_required=approval_required,
        approved_by_human=by_human,
        error=error,
    )


def _amend(ctx: RunContext, action: AgentAction, step: int) -> AmendOutcome:
    layer = ctx.layer
    if layer is None:
        return AmendOutcome(False, "There is no locked plan to extend.")
    suspicious = ctx.scout_map.flagged if ctx.scout_map else []
    amendment = propose_amendment(action.args, layer.manifest, ctx.engine, suspicious)
    if amendment.empty:
        return AmendOutcome(
            False,
            "Nothing new could be added: the request is already allowed or not allowed at all.",
        )
    context = ActionContext(
        tool=action.tool,
        args=amendment.added.model_dump(mode="json"),
        rule_id=AMENDMENT.rule_id,
        reason=AMENDMENT.reason,
        risk=amendment.risk,
        matched_rules=[
            MatchedRule(
                rule_id=AMENDMENT.rule_id,
                source=RuleSource.PLAN,
                decision=AMENDMENT.decision,
                risk=amendment.risk,
                reason=AMENDMENT.reason,
            )
        ],
        in_plan=False,
    )
    approval = amendment_approval(step, context, agent_reason=mask(action.reason) or None)
    if not ctx.ask(approval).approved or ctx.is_stopped():
        return AmendOutcome(False, "A human did not approve extending the plan.")
    layer.extend(amendment.added)
    ctx.emit(
        PlanEvent(
            run_id=ctx.run_id,
            phase=Phase.PLAN,
            ts=ctx.clock(),
            state=PlanState.AMENDED,
            plan=ctx.plan,
            added=amendment.added,
            approved_by_human=True,
        )
    )
    return AmendOutcome(True, "The plan now also allows: " + _manifest_text(amendment.added))


def _manifest_text(manifest: object) -> str:
    data = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else {}
    parts = [f"{k}: {', '.join(v)}" for k, v in data.items() if isinstance(v, list) and v]
    return "; ".join(parts) or "nothing"


def _evidence(ctx: RunContext) -> Evidence:
    workspace = ctx.run.workspace
    steps = ctx.steps
    diff = workspace_diff(
        ctx.original,
        workspace.path,
        is_secret=secret_check(ctx.engine.rulebook.context),
        left_out=workspace.left_out,
    )
    plan = ctx.plan
    rulebook = ctx.engine.rulebook
    return Evidence(
        brief=ctx.brief,
        done_criteria=plan.done_criteria if plan else [],
        diff=diff,
        original=ctx.original,
        workspace=workspace.path,
        write_paths=(ctx.layer.manifest.write_paths if ctx.layer else []),
        test_files=rulebook.context.test_files,
        test_config_files=rulebook.diff_guard.test_config_files,
        approved_writes=[
            str(e.args.get("path", "")).removeprefix("/workspace/")
            for e in steps
            if e.tool == "write_file" and e.approved and e.executed
        ],
        may_edit_tests=bool(ctx.layer and ctx.layer.manifest.may_edit_tests),
        blocked=[(e.tool, e.rule_id) for e in steps if e.blocked and not e.observed],
        hints=ctx.scout_map.hints() if ctx.scout_map else [],
    )


def report_input(ctx: RunContext, status: RunStatus) -> ReportInput:
    found = ctx.review
    delivery = ctx.delivery
    final = ctx.verdicts[-1] if ctx.verdicts else None
    return ReportInput(
        run_id=ctx.run_id,
        repo=ctx.options.repo,
        brief=ctx.brief,
        status=status,
        guard=ctx.options.guard,
        observe=ctx.run.mode is GateMode.OBSERVE,
        steps=ctx.steps,
        hops=ctx.run.egress,
        model=ctx.options.model,
        provider=ctx.options.provider,
        sandbox_runtime=ctx.options.sandbox_runtime,
        plan=ctx.plan,
        scout_flags=ctx.scout_map.flagged if ctx.scout_map else (),
        suspicious_pages=_suspicious_pages(ctx),
        verified=final.passed if final else None,
        findings=found.findings if found else (),
        unreviewed=found.unreviewed if found else False,
        delivered_files=delivery.patch.files if delivery else (),
        excluded=delivery.patch.excluded if delivery else (),
        withheld=delivery.blocked if delivery else False,
        released_by_human=delivery.released_by_human if delivery else False,
        budget=ctx.guards.budget(),
        tokens_by_role=dict(ctx.guards.meter.by_role),
        summary=ctx.agent_result.summary if ctx.agent_result else "",
        breaker_trips=ctx.guards.breaker.trips,
        checkpoints=ctx.checkpoints.latest,
        rollbacks=ctx.rollbacks,
    )


def _suspicious_pages(ctx: RunContext) -> list[str]:
    reader = ctx.run_reader
    return [url for url, flagged in reader.pages if flagged] if reader else []


# --- the harness ------------------------------------------------------------------------------


class Harness:
    """Runs one task through every stage on an *open* :class:`SandboxRun` that was built
    with this harness's hooks (:meth:`hooks_for`)."""

    def __init__(
        self,
        *,
        run_id: str,
        models: Models,
        run: SandboxRun,
        original: Path,
        engine: PolicyEngine,
        options: RunOptions,
        checkpoints: Checkpoints,
        reader: QuarantinedReader | None = None,
        human: Human = nobody,
        sink: Callable[[Event], None] = lambda _event: None,
        save_outputs: Callable[[Outputs], None] | None = None,
        clock: Callable[[], float] = time.time,
        guards: Guards | None = None,
    ) -> None:
        self.guards = guards or Guards(run_id, options.limits, wall=clock)
        self.ctx = RunContext(
            run_id=run_id,
            models=models,
            run=run,
            original=original,
            engine=engine,
            options=options,
            guards=self.guards,
            checkpoints=checkpoints,
            sink=sink,
            human=human,
            clock=clock,
        )
        self.ctx.run_reader = reader
        self.guards.emit = self.ctx.emit
        self._save_outputs = save_outputs
        self._stop_lock = threading.Lock()
        self._stop_recorded = False

    # --- control from other threads ----------------------------------------------------------

    def request_stop(self, by: str = "human") -> bool:
        """Set the stop flag and record one ``stopped`` event (False if already stopped).
        Call this *before* answering any waiting question."""
        with self._stop_lock:
            if self._stop_recorded:
                return False
            self._stop_recorded = True
            self.guards.request_stop()
        self.ctx.emit(
            StoppedEvent(
                run_id=self.ctx.run_id,
                phase=Phase.ACT,
                ts=self.ctx.clock(),
                by=by,
                summary="You stopped this run. Nothing will be delivered.",
                guard=self.ctx.options.guard,
            )
        )
        return True

    def request_rewind(self, checkpoint: int) -> None:
        self.ctx.request_rewind(checkpoint)

    @property
    def stopped(self) -> bool:
        return self.guards.stopped

    # --- the run -------------------------------------------------------------------------------

    def execute(self) -> HarnessResult:
        ctx = self.ctx
        ctx.checkpoints.attach(ctx.run.workspace.path, ctx.run.workspace.left_out)
        self.guards.start()
        status = self._pre()
        if not ctx.is_stopped():
            status = self._post(status)
        if ctx.is_stopped():
            status = RunStatus.STOPPED
        outputs = self._outputs(status)
        if outputs is not None and self._save_outputs is not None:
            self._save_outputs(outputs)
        summary = self._summary(status)
        ctx.emit(self._done(status, summary))
        return HarnessResult(
            status,
            summary,
            list(ctx.events),
            outputs,
            ctx.report,
            None if status is RunStatus.STOPPED else ctx.delivery,
            ctx.plan,
            list(ctx.verdicts),
        )

    def _pre(self) -> RunStatus:
        stages: list[Stage] = [
            UnderstandStage(),
            ScoutStage(),
            PlanStage(),
            ApproveStage(),
            LockStage(),
            ActStage(),
            VerifyStage(),
        ]
        for stage in stages:
            if self.ctx.is_stopped():
                return RunStatus.STOPPED
            outcome_status = self._safely(stage)
            if outcome_status is not None:
                return outcome_status
        return RunStatus.FINISHED

    def _post(self, status: RunStatus) -> RunStatus:
        for stage in (RedteamStage(), DeliverStage(), ReportStage(status)):
            if self.ctx.is_stopped():  # checked again before delivery and before each stage
                return RunStatus.STOPPED
            ended = self._safely(stage)
            if ended is RunStatus.STOPPED:
                return ended
            if ended is RunStatus.ERROR:
                status = ended
                if not isinstance(stage, ReportStage):
                    self._safely(ReportStage(status))
                break
        return status

    def _safely(self, stage: Stage) -> RunStatus | None:
        try:
            return stage.run(self.ctx)
        except Exception as problem:  # noqa: BLE001 - spec: any failure ends the run as error
            self.ctx.error = mask(f"{type(problem).__name__}: {problem}")[:500]
            return RunStatus.STOPPED if self.ctx.is_stopped() else RunStatus.ERROR

    def _outputs(self, status: RunStatus) -> Outputs | None:
        ctx = self.ctx
        if status is RunStatus.STOPPED or ctx.delivery is None or ctx.report is None:
            return None
        delivery = ctx.delivery
        return Outputs(
            patch=delivery.patch.text,
            summary_md=delivery.summary_md,
            blocked=delivery.blocked,
            report=ctx.report.data,
            report_md=ctx.report.markdown,
            learn={},
        )

    def _summary(self, status: RunStatus) -> str:
        ctx = self.ctx
        if status is RunStatus.STOPPED:
            return "You stopped this run. Nothing was delivered."
        if status is RunStatus.ERROR and ctx.error:
            return f"The run failed: {ctx.error}"
        if ctx.agent_result is not None and ctx.agent_result.summary:
            return mask(ctx.agent_result.summary)
        return f"The run ended: {status.value}."

    def _done(self, status: RunStatus, summary: str) -> DoneEvent:
        ctx = self.ctx
        steps = ctx.steps
        delivery = None if status is RunStatus.STOPPED else ctx.delivery
        final = ctx.verdicts[-1] if ctx.verdicts else None
        withheld = bool(delivery and delivery.blocked)
        return DoneEvent(
            run_id=ctx.run_id,
            phase=Phase.REPORT,
            ts=ctx.clock(),
            status=status,
            summary=summary,
            steps=len(steps),
            guard=ctx.options.guard,
            outcome=outcome(status, withheld=withheld),
            provider=ctx.options.provider,
            model=ctx.options.model,
            sandbox_runtime=ctx.options.sandbox_runtime,
            counts=_counts(steps),
            plan=ctx.plan,
            verified=final.passed if final else None,
            retries=ctx.retries,
            breaker_trips=ctx.guards.breaker.trips,
            rollbacks=ctx.rollbacks,
            checkpoints=ctx.checkpoints.latest,
            delivered=delivery is not None and not delivery.blocked,
            delivery_blocked=withheld,
            released_by_human=bool(delivery and delivery.released_by_human),
            changed_files=list(delivery.patch.files) if delivery else [],
            final_tests=final.tests if final else None,
            scout_flags=ctx.scout_map.flagged if ctx.scout_map else [],
            suspicious_pages=_suspicious_pages(ctx),
            trifecta=ctx.report.event.trifecta if ctx.report else None,
        )


def _counts(steps: Sequence[StepEvent]) -> DecisionCounts:
    return DecisionCounts(
        allowed=sum(1 for e in steps if e.decision is Decision.ALLOW),
        denied=sum(1 for e in steps if e.decision is Decision.DENY),
        asked=sum(1 for e in steps if e.decision is Decision.ASK),
        injection_tagged=sum(1 for e in steps if e.tags),
    )
