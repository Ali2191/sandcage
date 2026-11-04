"""The act loop: ask the model for one action, run it through the gate, feed the result back.

The loop knows nothing about the harness. Everything run-level reaches it through hooks:

- ``gate(action)``: before each model call (``None``) and again before each action; may
  return a final status to stop the run (stop flag, budgets, loop detection);
- ``after_step(event)``: after each step; may return a final status (the breaker);
- ``annotate(event)``: adds run-level fields to each step event (budget, breaker counts);
- ``amender(action, step)``: handles ``amend_plan`` (validation and a human's answer).

Per turn: gate(None) → model (masked, compacted context) → parse (one retry after an
invalid reply, then ``invalid_reply``) → gate(action) → count the step (over the limit is
``budget.steps``) → execute through the gate → an ask goes to the approver as one typed
:class:`Approval` (an approver error counts as no) → event → after_step. ``finish`` ends
the run only when it was actually executed. A model error ends the run as ``error`` with a
masked message.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import JsonValue

from sandcage.agent import prompts
from sandcage.agent.compact import CONTEXT_BUDGET, compact
from sandcage.agent.llm import ChatClient, Message, ModelError, Usage
from sandcage.agent.protocol import ActionParseError, AgentAction, parse_action
from sandcage.contract.approval import ActionContext, Approval, Resolution, action_approval
from sandcage.contract.common import MatchedRule, Phase, Plan
from sandcage.contract.events import (
    OUTPUT_PREVIEW_LIMIT,
    ReaderInfo,
    RunStatus,
    StepEvent,
)
from sandcage.contract.wording import SecretCheck, blocked_sentence
from sandcage.masking import mask
from sandcage.plan.layer import AMENDMENT
from sandcage.policy import Decision, PolicyDecision, RuleSource
from sandcage.sandbox.run import StepResult

MAX_STEPS: Final = 25
MASK_MARK: Final = "[MASKED:"


class Executor(Protocol):
    """The gate plus the sandbox: :class:`sandcage.sandbox.run.SandboxRun`, or a fake."""

    def execute(self, raw: object) -> StepResult: ...

    def execute_approved(self, raw: object) -> StepResult: ...


@dataclass(frozen=True)
class AmendOutcome:
    approved: bool
    detail: str


Gate = Callable[[AgentAction | None], RunStatus | None]
AfterStep = Callable[[StepEvent], RunStatus | None]
Annotate = Callable[[StepEvent], StepEvent]
Amender = Callable[[AgentAction, int], AmendOutcome]
Approver = Callable[[Approval], Resolution]
Emit = Callable[[StepEvent], None]
UsageSink = Callable[[Usage | None, int], None]


def deny_all(approval: Approval) -> Resolution:
    return approval.timeout_resolution()


@dataclass
class Hooks:
    gate: Gate | None = None
    after_step: AfterStep | None = None
    annotate: Annotate | None = None
    amender: Amender | None = None


@dataclass
class AgentResult:
    status: RunStatus
    summary: str
    steps: int
    events: list[StepEvent] = field(default_factory=list)


@dataclass
class Agent:
    client: ChatClient
    executor: Executor
    run_id: str
    is_secret: SecretCheck
    guard: bool = True
    max_steps: int = MAX_STEPS
    approver: Approver = deny_all
    emit: Emit | None = None
    hooks: Hooks = field(default_factory=Hooks)
    on_usage: UsageSink | None = None
    context_budget: int = CONTEXT_BUDGET
    clock: Callable[[], float] = time.time
    messages: list[Message] = field(default_factory=list)
    steps: int = 0
    events: list[StepEvent] = field(default_factory=list)

    # --- entry points --------------------------------------------------------------------

    def run_task(self, task: str, plan: Plan | None = None) -> AgentResult:
        self.messages = [
            {"role": "system", "content": prompts.system_prompt(guard=self.guard)},
            {"role": "user", "content": prompts.task_message(task, plan)},
        ]
        return self._loop()

    def continue_task(self, feedback: str) -> AgentResult:
        """More work after feedback (the verifier's reasons); steps keep counting."""
        if not self.messages:
            raise RuntimeError("continue_task needs a task that was started")
        self.messages.append({"role": "user", "content": mask(feedback)})
        return self._loop()

    def note(self, text: str) -> None:
        """Tell the model something outside its own actions (a human rewind)."""
        self.messages.append({"role": "user", "content": prompts.note_message(text)})

    # --- the loop --------------------------------------------------------------------------

    def _stop(self, status: RunStatus, summary: str) -> AgentResult:
        return AgentResult(status, summary, self.steps, list(self.events))

    def _loop(self) -> AgentResult:
        retried = False
        while True:
            stopped = self._gate(None)
            if stopped is not None:
                return self._stop(stopped, "stopped by the harness before the next model call")
            try:
                completion = self.client.complete(compact(self.messages, self.context_budget))
            except ModelError as problem:
                return self._stop(RunStatus.ERROR, mask(str(problem)))
            if self.on_usage is not None:
                self.on_usage(completion.usage, sum(len(m["content"]) for m in self.messages))
            try:
                action = parse_action(completion.text)
            except ActionParseError as problem:
                if retried:
                    return self._stop(RunStatus.INVALID_REPLY, "the model sent two invalid replies")
                retried = True
                self.messages.append({"role": "assistant", "content": mask(completion.text)[:2000]})
                self.messages.append(
                    {"role": "user", "content": prompts.invalid_reply_message(str(problem))}
                )
                continue
            retried = False
            stopped = self._gate(action)
            if stopped is not None:
                return self._stop(stopped, "stopped by the harness before the action ran")
            if self.steps >= self.max_steps:
                return self._stop(
                    RunStatus.BUDGET_STEPS, f"reached the {self.max_steps}-step limit"
                )
            self.steps += 1
            event, finished = self._step(action)
            if finished:
                return self._stop(RunStatus.FINISHED, mask(_summary(action)))
            if self.hooks.after_step is not None:
                stopped = self.hooks.after_step(event)
                if stopped is not None:
                    return self._stop(stopped, "stopped by the harness after a step")

    def _gate(self, action: AgentAction | None) -> RunStatus | None:
        return self.hooks.gate(action) if self.hooks.gate is not None else None

    # --- one step --------------------------------------------------------------------------

    def _step(self, action: AgentAction) -> tuple[StepEvent, bool]:
        step = self.steps
        self.messages.append({"role": "assistant", "content": prompts.action_text(action)})
        if action.tool == "amend_plan":
            return self._amend(action, step), False
        raw = action.to_raw()
        result = self.executor.execute(raw)
        approved: bool | None = None
        problem: str | None = None
        decision = result.decision
        if decision.decision is Decision.ASK and not result.observed:
            approved, problem = self._ask(step, action, result)
            if approved:
                result = self.executor.execute_approved(raw)
        executed = result.executed
        blocked = not executed and not result.observed
        if executed:
            message = prompts.result_message(step, action, result.result, guard=self.guard)
        elif decision.decision is Decision.ASK:
            message = prompts.not_approved_message(step)
        else:
            message = prompts.blocked_message(step, decision.reason)
        self.messages.append({"role": "user", "content": message})
        event = self._event(step, action, result, approved=approved, blocked=blocked)
        if problem is not None:
            event = event.model_copy(update={"output_preview": problem})
        return self._record(event), action.tool == "finish" and executed

    def _ask(self, step: int, action: AgentAction, result: StepResult) -> tuple[bool, str | None]:
        """The human's answer, and a note when asking failed (which counts as no)."""
        approval = action_approval(
            step, _context(action, result), agent_reason=mask(action.reason) or None
        )
        try:
            return self.approver(approval).approved, None
        except Exception as error:  # noqa: BLE001 - spec: an approver failure counts as no
            return (
                False,
                f"Asking for approval failed ({type(error).__name__}), so it counts as no.",
            )

    def _amend(self, action: AgentAction, step: int) -> StepEvent:
        if self.hooks.amender is None:
            outcome = AmendOutcome(False, "Plan amendments are not available in this run.")
        else:
            try:
                outcome = self.hooks.amender(action, step)
            except Exception as error:  # noqa: BLE001 - an amender failure counts as no
                outcome = AmendOutcome(
                    False, f"The amendment could not be handled ({type(error).__name__})."
                )
        self.messages.append(
            {
                "role": "user",
                "content": prompts.amendment_message(
                    step, approved=outcome.approved, detail=outcome.detail
                ),
            }
        )
        event = StepEvent(
            run_id=self.run_id,
            phase=Phase.PLAN,
            ts=self.clock(),
            step=step,
            guard=self.guard,
            tool=action.tool,
            args=_masked(action.args),
            agent_reason=mask(action.reason),
            decision=Decision.ASK,
            rule_id=AMENDMENT.rule_id,
            reason=AMENDMENT.reason,
            risk=AMENDMENT.risk,
            matched_rules=[
                MatchedRule(
                    rule_id=AMENDMENT.rule_id,
                    source=RuleSource.PLAN,
                    decision=AMENDMENT.decision,
                    risk=AMENDMENT.risk,
                    reason=AMENDMENT.reason,
                )
            ],
            approved=outcome.approved,
            executed=outcome.approved,
            blocked=not outcome.approved,
            blocked_sentence=None if outcome.approved else "Tried to extend the locked plan",
            output_preview=mask(outcome.detail)[:OUTPUT_PREVIEW_LIMIT],
        )
        return self._record(event)

    def _record(self, event: StepEvent) -> StepEvent:
        if self.hooks.annotate is not None:
            event = self.hooks.annotate(event)
        self.events.append(event)
        if self.emit is not None:
            self.emit(event)
        return event

    def _event(
        self,
        step: int,
        action: AgentAction,
        result: StepResult,
        *,
        approved: bool | None,
        blocked: bool,
    ) -> StepEvent:
        decision = result.decision
        output = result.result
        preview = _preview(output)
        return StepEvent(
            run_id=self.run_id,
            phase=Phase.ACT,
            ts=self.clock(),
            step=step,
            guard=self.guard,
            tool=action.tool,
            args=_masked(action.args) if result.args is None else _as_args(result.args),
            agent_reason=mask(action.reason),
            decision=decision.decision,
            rule_id=decision.rule_id,
            reason=mask(decision.reason),
            risk=decision.risk,
            tags=list(decision.tags),
            source=decision.source,
            taint_sources=list(decision.taint_sources),
            matched_rules=[
                MatchedRule.model_validate(hit.to_dict()) for hit in decision.matched_rules
            ],
            in_plan=decision.in_plan,
            observed=result.observed,
            approved=approved,
            executed=result.executed,
            blocked=blocked,
            blocked_sentence=(
                blocked_sentence(
                    action.tool, action.args, decision.rule_id, is_secret=self.is_secret
                )
                if blocked
                else None
            ),
            output_preview=preview,
            checkpoint=_checkpoint(output),
            masked_secrets=MASK_MARK in json.dumps(output) if output is not None else False,
            reader=_reader(output),
        )


# --- helpers -------------------------------------------------------------------------------


def _context(action: AgentAction, result: StepResult) -> ActionContext:
    decision: PolicyDecision = result.decision
    return ActionContext(
        tool=action.tool,
        args=_as_args(result.args) if result.args is not None else _masked(action.args),
        rule_id=decision.rule_id,
        reason=mask(decision.reason),
        risk=decision.risk,
        matched_rules=[MatchedRule.model_validate(hit.to_dict()) for hit in decision.matched_rules],
        in_plan=decision.in_plan,
        tags=list(decision.tags),
        source=decision.source,
        taint_sources=list(decision.taint_sources),
    )


def _masked(args: dict[str, JsonValue]) -> dict[str, JsonValue]:
    masked = json.loads(mask(json.dumps(args)))
    return masked if isinstance(masked, dict) else {}


def _as_args(args: JsonValue) -> dict[str, JsonValue]:
    return args if isinstance(args, dict) else {}


def _summary(action: AgentAction) -> str:
    summary = action.args.get("summary", "")
    return summary if isinstance(summary, str) else ""


def _preview(output: JsonValue | None) -> str:
    if not isinstance(output, dict):
        return ""
    command_output = output.get("output")
    if "exit_code" in output and isinstance(command_output, str):
        return mask(command_output)[-OUTPUT_PREVIEW_LIMIT:]  # a command's summary is at the end
    for key in ("content", "answer", "error", "summary"):
        value = output.get(key)
        if isinstance(value, str):
            return mask(value)[:OUTPUT_PREVIEW_LIMIT]
    entries = output.get("entries")
    if isinstance(entries, list):
        names = [
            name
            for entry in entries
            if isinstance(entry, dict) and isinstance(name := entry.get("name"), str)
        ]
        return mask("\n".join(names))[:OUTPUT_PREVIEW_LIMIT]
    return mask(json.dumps(output))[:OUTPUT_PREVIEW_LIMIT]


def _checkpoint(output: JsonValue | None) -> int | None:
    if isinstance(output, dict):
        value = output.get("checkpoint")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _reader(output: JsonValue | None) -> ReaderInfo | None:
    if not isinstance(output, dict) or "answer" not in output or "url" not in output:
        return None
    url, answer = output.get("url"), output.get("answer")
    flagged = output.get("contains_instructions")
    if isinstance(url, str) and isinstance(answer, str) and isinstance(flagged, bool):
        return ReaderInfo(url=url, answer=answer, contains_instructions=flagged)
    return None
