"""Every event a run can emit: the one source of truth for the store, the stream and the panel.

An event is a flat JSON object. ``kind`` says which model it is; ``id`` is the store's
ordered id (also the SSE id; 0 until stored); every event carries ``run_id``, ``phase``
and ``ts`` (Unix seconds), and most carry a ``budget`` snapshot.

Readers parse with :func:`parse_event`: unknown fields are ignored and an unknown kind
comes back as :class:`UnknownEvent` instead of failing, so an older reader survives a
newer writer. Writers build the models, so they can only emit what is declared here.
The JSON Schema and the panel's TypeScript types are generated from these models
(``python -m sandcage.contract``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from sandcage.contract.approval import Approval, ApprovalKind, RedteamFinding
from sandcage.contract.common import (
    ContractModel,
    Decision,
    Manifest,
    MatchedRule,
    Phase,
    Plan,
    Risk,
)

OUTPUT_PREVIEW_LIMIT: Final = 300


class RunStatus(StrEnum):
    """A run's status: ``running`` while it lives, then exactly one final status."""

    RUNNING = "running"
    FINISHED = "finished"
    VERIFY_FAILED = "verify_failed"
    NEEDS_CLARIFICATION = "needs_clarification"
    PLAN_INVALID = "plan_invalid"
    PLAN_REJECTED = "plan_rejected"
    BUDGET_STEPS = "budget.steps"
    BUDGET_TOKENS = "budget.tokens"
    BUDGET_TIME = "budget.time"
    LOOP_REPEAT = "loop.repeat"
    KILLED = "killed"
    INVALID_REPLY = "invalid_reply"
    ERROR = "error"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class Budget(ContractModel):
    """Usage so far against the run's limits. ``tokens_reported`` is false when any count
    was estimated (about four characters per token); estimated numbers are never shown as
    measured."""

    steps: int
    max_steps: int
    tokens: int
    max_tokens: int
    tokens_reported: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float
    max_seconds: float


class EventBase(ContractModel):
    id: int = 0
    run_id: str
    phase: Phase
    ts: float
    budget: Budget | None = None


# --- understand / scout / plan ---------------------------------------------------------


class UnderstandState(StrEnum):
    UNDERSTOOD = "understood"
    ASKING = "asking"
    ANSWERED = "answered"
    UNANSWERED = "unanswered"


class UnderstandEvent(EventBase):
    kind: Literal["understand"] = "understand"
    state: UnderstandState
    task: str
    done_criteria: list[str] = Field(default_factory=list)
    question: str | None = None
    answer: str | None = None
    valid: bool = True


class ScoutFlag(ContractModel):
    path: str
    reason: str


class ScoutEvent(EventBase):
    kind: Literal["scout"] = "scout"
    flags: list[ScoutFlag] = Field(default_factory=list)
    files_mapped: int = 0
    model_calls: int = 0
    skipped: list[str] = Field(default_factory=list)
    test_command: str | None = None
    test_command_source: str | None = None
    ignored_hints: list[str] = Field(default_factory=list)


class PlanState(StrEnum):
    PROPOSED = "proposed"
    INVALID = "invalid"
    REJECTED = "rejected"
    APPROVED = "approved"
    LOCKED = "locked"
    AMENDED = "amended"


class PlanEvent(EventBase):
    kind: Literal["plan"] = "plan"
    state: PlanState
    plan: Plan | None = None
    scout_flags: list[str] = Field(default_factory=list)
    approval_required: bool = False
    approved_by_human: bool = False
    added: Manifest | None = None
    error: str | None = None


# --- approvals -------------------------------------------------------------------------


class ApprovalNeededEvent(EventBase):
    """A human is asked. The panel's banner renders from ``approval`` alone."""

    kind: Literal["approval_needed"] = "approval_needed"
    approval: Approval


class ResolvedBy(StrEnum):
    HUMAN = "human"
    TIMEOUT = "timeout"
    STOP = "stop"
    ERROR = "error"


class ApprovalResolvedEvent(EventBase):
    """The pending approval got its answer (a timeout, a stop or an error count as the
    approval's ``on_timeout`` choice or as no)."""

    kind: Literal["approval_resolved"] = "approval_resolved"
    approval_id: str
    approval_kind: ApprovalKind
    step: int
    choice: str
    approved: bool
    by: ResolvedBy
    answer: str | None = None


# --- act -------------------------------------------------------------------------------


class ReaderInfo(ContractModel):
    """What the quarantined reader said about a fetched page (the agent saw only this)."""

    url: str
    answer: str
    contains_instructions: bool


class BreakerCounts(ContractModel):
    denials: int
    injection_denials: int
    trips: int


class StepEvent(EventBase):
    """One agent action and the gate's decision on it. A step is *blocked* when it was
    denied, or asked and not approved; ``blocked_sentence`` is then Sandcage's own
    sentence for it."""

    kind: Literal["step"] = "step"
    step: int
    guard: bool
    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    agent_reason: str = ""
    decision: Decision
    rule_id: str
    reason: str
    risk: Risk
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    taint_sources: list[str] = Field(default_factory=list)
    matched_rules: list[MatchedRule] = Field(default_factory=list)
    in_plan: bool = True
    observed: bool = False
    approved: bool | None = None
    executed: bool
    blocked: bool
    blocked_sentence: str | None = None
    output_preview: str = ""
    checkpoint: int | None = None
    masked_secrets: bool = False
    reader: ReaderInfo | None = None
    breaker: BreakerCounts | None = None


class CheckpointState(StrEnum):
    SAVED = "saved"
    RESTORED = "restored"
    FAILED = "failed"


class CheckpointEvent(EventBase):
    kind: Literal["checkpoint"] = "checkpoint"
    checkpoint: int
    state: CheckpointState
    files: list[str] = Field(default_factory=list)
    step: int | None = None
    restored_by: Literal["agent", "human"] | None = None
    error: str | None = None


class BreakerState(StrEnum):
    TRIPPED = "tripped"
    RESUMED = "resumed"
    KILLED = "killed"


class BreakerEvent(EventBase):
    kind: Literal["breaker"] = "breaker"
    state: BreakerState
    reason: str
    source: str | None = None
    denials: int
    injection_denials: int
    trips: int


class GuardEvent(EventBase):
    kind: Literal["guard"] = "guard"
    stop_reason: Literal["budget.steps", "budget.tokens", "budget.time", "loop.repeat"]
    detail: str


# --- verify / red-team / deliver / report / learn ----------------------------------------


class VerifyCheck(ContractModel):
    name: str
    passed: bool
    detail: str = ""


class TestRun(ContractModel):
    """A test run's outcome, without its output."""

    __test__: ClassVar[bool] = False  # not a pytest test class

    command: str
    passed: bool
    exit_code: int | None = None
    timed_out: bool = False


class VerifyEvent(EventBase):
    kind: Literal["verify"] = "verify"
    attempt: int
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    checks: list[VerifyCheck] = Field(default_factory=list)
    judged_by_model: bool = False
    tests: TestRun | None = None


class RedteamEvent(EventBase):
    kind: Literal["redteam"] = "redteam"
    findings: list[RedteamFinding] = Field(default_factory=list)
    blocked: bool
    unreviewed: bool = False
    skipped_reason: str | None = None


class ExcludedFile(ContractModel):
    path: str
    reason: str


class DeliverEvent(EventBase):
    kind: Literal["deliver"] = "deliver"
    files: list[str] = Field(default_factory=list)
    excluded: list[ExcludedFile] = Field(default_factory=list)
    blocked: bool
    released_by_human: bool = False
    patch_bytes: int = 0


class Trifecta(ContractModel):
    """The lethal trifecta: were private data, untrusted content and a way out ever all
    present at the same step?"""

    private_data: bool
    untrusted_content: bool
    way_out: bool
    all_at_once: bool


class CapabilityCount(ContractModel):
    capability: str
    attempted: int
    executed: int
    blocked: int


class ReportEvent(EventBase):
    kind: Literal["report"] = "report"
    trifecta: Trifecta
    blocks: int
    capabilities: list[CapabilityCount] = Field(default_factory=list)


class LearnSuggestion(ContractModel):
    id: str
    kind: Literal["suspicious_file", "ask_host", "deny_command"]
    rule_id: str
    explanation: str
    target: str


class LearnEvent(EventBase):
    kind: Literal["learn"] = "learn"
    suggestions: list[LearnSuggestion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --- end of a run ------------------------------------------------------------------------


class StoppedEvent(EventBase):
    kind: Literal["stopped"] = "stopped"
    by: str
    summary: str
    guard: bool


class DecisionCounts(ContractModel):
    allowed: int = 0
    denied: int = 0
    asked: int = 0
    injection_tagged: int = 0


class DoneEvent(EventBase):
    """Always the last event of a run: the final picture the run list and report use."""

    kind: Literal["done"] = "done"
    status: RunStatus
    summary: str
    steps: int
    guard: bool
    provider: str | None = None
    model: str | None = None
    sandbox_runtime: str | None = None
    counts: DecisionCounts = Field(default_factory=DecisionCounts)
    plan: Plan | None = None
    verified: bool | None = None
    retries: int = 0
    breaker_trips: int = 0
    rollbacks: int = 0
    checkpoints: int = 0
    delivered: bool = False
    delivery_blocked: bool = False
    released_by_human: bool = False
    changed_files: list[str] = Field(default_factory=list)
    final_tests: TestRun | None = None
    scout_flags: list[str] = Field(default_factory=list)
    suspicious_pages: list[str] = Field(default_factory=list)
    trifecta: Trifecta | None = None


Event = Annotated[
    UnderstandEvent
    | ScoutEvent
    | PlanEvent
    | ApprovalNeededEvent
    | ApprovalResolvedEvent
    | StepEvent
    | CheckpointEvent
    | BreakerEvent
    | GuardEvent
    | VerifyEvent
    | RedteamEvent
    | DeliverEvent
    | ReportEvent
    | LearnEvent
    | StoppedEvent
    | DoneEvent,
    Field(discriminator="kind"),
]

EVENT_ADAPTER: Final[TypeAdapter[Event]] = TypeAdapter(Event)

EVENT_KINDS: Final = (
    "understand",
    "scout",
    "plan",
    "approval_needed",
    "approval_resolved",
    "step",
    "checkpoint",
    "breaker",
    "guard",
    "verify",
    "redteam",
    "deliver",
    "report",
    "learn",
    "stopped",
    "done",
)


class UnknownEvent(ContractModel):
    """An event of a kind this reader does not know (a newer writer). Kept, not dropped."""

    kind: str
    id: int = 0
    run_id: str = ""
    raw: dict[str, JsonValue] = Field(default_factory=dict)


class EventParseError(ValueError):
    """A known event kind whose payload does not fit the contract."""


def parse_event(raw: object) -> Event | UnknownEvent:
    """Read one event tolerantly: extra fields are ignored, an unknown kind is kept as
    :class:`UnknownEvent`, and a known kind that does not fit raises
    :class:`EventParseError`."""
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        raise EventParseError("an event must be an object with a 'kind'")
    kind = raw["kind"]
    if kind not in EVENT_KINDS:
        event_id = raw.get("id")
        run_id = raw.get("run_id")
        return UnknownEvent(
            kind=kind,
            id=event_id if isinstance(event_id, int) and not isinstance(event_id, bool) else 0,
            run_id=run_id if isinstance(run_id, str) else "",
            raw=raw,
        )
    try:
        return EVENT_ADAPTER.validate_python(raw)
    except ValidationError as problem:
        raise EventParseError(f"a '{kind}' event does not fit the contract: {problem}") from None


def dump_event(event: Event) -> dict[str, JsonValue]:
    """The JSON form of an event (enums as their values)."""
    dumped: dict[str, JsonValue] = EVENT_ADAPTER.dump_python(event, mode="json")
    return dumped
