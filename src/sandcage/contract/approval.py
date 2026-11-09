"""One record for every question Sandcage puts to a human.

An earlier design had six special cases (action ask, plan approval, plan amendment, breaker
resume, red-team release, clarifying question), each with its own backend path, API
validation and panel banner. Here each is one :class:`Approval`: the banner's exact
words, the choices with their shortcut keys, an optional free-text box, the timeout and
which choice a timeout picks. The panel renders every banner from this record alone, so
a kind can never exist without a banner.

The wording is the catalogue from Design/PAGES.md section 6. The builders below are the
only place it is written; tests pin it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, JsonValue

from sandcage.contract.common import ContractModel, MatchedRule, Phase, Plan, Risk
from sandcage.masking import mask

APPROVAL_TIMEOUT_SECONDS: Final = 120
ANSWER_MAX_LENGTH: Final = 1000


class ApprovalKind(StrEnum):
    ACTION = "action"
    PLAN = "plan"
    AMENDMENT = "amendment"
    BREAKER = "breaker"
    REDTEAM = "redteam"
    QUESTION = "question"


class Layout(StrEnum):
    """``standard``: text left, buttons right. ``question``: the box spans the width and
    the buttons sit underneath."""

    STANDARD = "standard"
    QUESTION = "question"


class Choice(ContractModel):
    """One button. ``approves`` is what the backend does with it (yes or no)."""

    id: str
    label: str
    approves: bool
    primary: bool = False
    shortcut: str | None = None


class FreeText(ContractModel):
    """A text box whose content is sent with an approving choice."""

    label: str
    placeholder: str
    max_length: int = ANSWER_MAX_LENGTH
    required: bool = True


class CodeBlock(ContractModel):
    type: Literal["code"] = "code"
    text: str


class TextBlock(ContractModel):
    type: Literal["text"] = "text"
    text: str
    muted: bool = False


class Fact(ContractModel):
    label: str
    value: str
    mono: bool = True


class FactsBlock(ContractModel):
    type: Literal["facts"] = "facts"
    rows: list[Fact]


class FindingItem(ContractModel):
    location: str
    text: str


class FindingsBlock(ContractModel):
    type: Literal["findings"] = "findings"
    items: list[FindingItem]


BodyBlock = Annotated[
    CodeBlock | TextBlock | FactsBlock | FindingsBlock, Field(discriminator="type")
]


class ActionContext(ContractModel):
    """The gated action behind an ``action`` or ``amendment`` approval."""

    tool: str
    args: dict[str, JsonValue]
    rule_id: str
    reason: str
    risk: Risk
    matched_rules: list[MatchedRule] = Field(default_factory=list)
    in_plan: bool = True
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    taint_sources: list[str] = Field(default_factory=list)


class Approval(ContractModel):
    """A question to a human: everything a banner needs, and how to answer it."""

    id: str
    kind: ApprovalKind
    step: int
    phase: Phase
    eyebrow: str
    headline: str
    body: list[BodyBlock] = Field(default_factory=list)
    note: str | None = None
    choices: list[Choice]
    free_text: FreeText | None = None
    layout: Layout = Layout.STANDARD
    timeout_seconds: int = APPROVAL_TIMEOUT_SECONDS
    on_timeout: str
    timeout_text: str
    action: ActionContext | None = None

    def choice(self, choice_id: str) -> Choice:
        for choice in self.choices:
            if choice.id == choice_id:
                return choice
        known = ", ".join(choice.id for choice in self.choices)
        raise ApprovalAnswerError(f"'{choice_id}' is not a choice here (choices: {known})")

    def resolve(self, choice_id: str, answer: str | None = None) -> Resolution:
        """Check a human's answer against this approval. Raises :class:`ApprovalAnswerError`
        with a message fit to show when the answer does not fit."""
        choice = self.choice(choice_id)
        text: str | None = None
        if answer is not None and self.free_text is None:
            raise ApprovalAnswerError("this question takes no written answer")
        if self.free_text is not None and choice.approves:
            text = (answer or "").strip()
            if self.free_text.required and not text:
                raise ApprovalAnswerError("an answer is needed")
            if len(text) > self.free_text.max_length:
                raise ApprovalAnswerError(
                    f"the answer is longer than {self.free_text.max_length} characters"
                )
            text = mask(text)
        return Resolution(choice=choice.id, approved=choice.approves, answer=text)

    def timeout_resolution(self) -> Resolution:
        choice = self.choice(self.on_timeout)
        return Resolution(choice=choice.id, approved=choice.approves, answer=None)


class Resolution(ContractModel):
    choice: str
    approved: bool
    answer: str | None = None


class ApprovalAnswerError(ValueError):
    """A human's answer does not fit the pending approval."""


# --- builders (the wording catalogue) -------------------------------------------------

NEEDS_YOUR_CALL: Final = "NEEDS YOUR CALL"
_DENY_TIMEOUT: Final = "timed out: counts as deny"


def _allow_deny(primary_label: str) -> list[Choice]:
    return [
        Choice(id="approve", label=primary_label, approves=True, primary=True, shortcut="A"),
        Choice(id="deny", label="Deny", approves=False, shortcut="D"),
    ]


def action_line(tool: str, args: dict[str, JsonValue]) -> str:
    """How an action is shown in mono: ``$ pip install requests``, ``read README.md``."""
    if tool == "run_command":
        return f"$ {args.get('command', '')}"
    if tool in {"read_file", "write_file", "list_dir"}:
        verb = {"read_file": "read", "write_file": "write", "list_dir": "list"}[tool]
        return f"{verb} {args.get('path', '.')}"
    if tool == "http_get":
        return f"fetch {args.get('url', '')}"
    if tool == "amend_plan":
        return "amend the plan: " + ", ".join(
            f"{key} {value}" for key, value in sorted(args.items()) if key != "reason"
        )
    return tool


def _action_body(context: ActionContext, agent_reason: str | None) -> list[BodyBlock]:
    body: list[BodyBlock] = [CodeBlock(text=action_line(context.tool, context.args))]
    if agent_reason:
        body.append(TextBlock(text=agent_reason, muted=True))
    return body


def action_approval(
    step: int, context: ActionContext, *, agent_reason: str | None = None
) -> Approval:
    return Approval(
        id=f"action:{step}",
        kind=ApprovalKind.ACTION,
        step=step,
        phase=Phase.ACT,
        eyebrow=NEEDS_YOUR_CALL,
        headline="The agent wants to do something the plan doesn't cover.",
        body=_action_body(context, agent_reason),
        choices=_allow_deny("Allow once"),
        on_timeout="deny",
        timeout_text=_DENY_TIMEOUT,
        action=context,
    )


def amendment_approval(
    step: int, context: ActionContext, *, agent_reason: str | None = None
) -> Approval:
    return Approval(
        id=f"amendment:{step}",
        kind=ApprovalKind.AMENDMENT,
        step=step,
        phase=Phase.APPROVE,
        eyebrow=NEEDS_YOUR_CALL,
        headline="The agent wants to extend the locked plan.",
        body=_action_body(context, agent_reason),
        choices=_allow_deny("Allow once"),
        on_timeout="deny",
        timeout_text=_DENY_TIMEOUT,
        action=context,
    )


def plan_approval(plan: Plan) -> Approval:
    manifest = plan.manifest
    headline = "Approve the plan before work starts."
    if plan.risk in {Risk.HIGH, Risk.CRITICAL}:
        headline = "Approve the plan before work starts, it's high risk."
    body: list[BodyBlock] = [
        FactsBlock(
            rows=[
                Fact(label="May edit", value=", ".join(manifest.write_paths) or "nothing"),
                Fact(label="May run", value=", ".join(manifest.commands) or "nothing"),
                Fact(label="Network", value=", ".join(manifest.network) or "none"),
            ]
        )
    ]
    if plan.risk_reasons:
        body.append(TextBlock(text="; ".join(plan.risk_reasons) + ".", muted=True))
    return Approval(
        id="plan:0",
        kind=ApprovalKind.PLAN,
        step=0,
        phase=Phase.APPROVE,
        eyebrow="APPROVE THE PLAN",
        headline=headline,
        body=body,
        choices=_allow_deny("Approve plan"),
        on_timeout="deny",
        timeout_text=_DENY_TIMEOUT,
    )


def breaker_approval(step: int, *, source: str | None, tried: Sequence[str]) -> Approval:
    """``tried`` holds Sandcage's blocked sentences without "Tried to", oldest first, e.g.
    "send your .env to collector.example.net"."""
    if source:
        headline = f"Something in {source} is steering the agent, so Sandcage paused it."
    else:
        headline = "The agent keeps trying things it isn't allowed to, so Sandcage paused it."
    count = len(tried)
    times = "once" if count == 1 else f"{count} times"
    line = f"Blocked {times} after it read {source}." if source else f"Blocked {times}."
    if tried:
        line += " It tried to " + tried[0]
        if count >= 2:
            line += ", then " + tried[1]
        if count > 2:
            line += f", and {count - 2} more"
        line += "."
    return Approval(
        id=f"breaker:{step}",
        kind=ApprovalKind.BREAKER,
        step=step,
        phase=Phase.ACT,
        eyebrow="CIRCUIT BREAKER",
        headline=headline,
        body=[TextBlock(text=line)],
        note="Resuming marks the source untrusted. Ending destroys the sandbox.",
        choices=[
            Choice(id="resume", label="Resume", approves=True, primary=True, shortcut="R"),
            Choice(id="end", label="End run", approves=False, shortcut="K"),
        ],
        on_timeout="end",
        timeout_text="timed out: counts as end",
    )


class RedteamFinding(ContractModel):
    severity: Risk
    file: str
    line: int | None = None
    explanation: str


def redteam_approval(step: int, *, findings: Sequence[RedteamFinding], reason: str) -> Approval:
    """Release a patch the red-team blocked. No shortcuts: releasing is risky on purpose."""
    serious = [f for f in findings if f.severity in {Risk.HIGH, Risk.CRITICAL}]
    body: list[BodyBlock]
    if serious:
        body = [
            FindingsBlock(
                items=[
                    FindingItem(
                        location=f.file if f.line is None else f"{f.file}:{f.line}",
                        text=f.explanation,
                    )
                    for f in serious
                ]
            )
        ]
    else:
        body = [TextBlock(text=reason)]
    return Approval(
        id=f"redteam:{step}",
        kind=ApprovalKind.REDTEAM,
        step=step,
        phase=Phase.REDTEAM,
        eyebrow="RED-TEAM",
        headline="The red-team found high-severity issues. Release the patch anyway?",
        body=body,
        choices=[
            Choice(id="keep_blocked", label="Keep it blocked", approves=False, primary=True),
            Choice(id="release", label="Release patch", approves=True),
        ],
        on_timeout="keep_blocked",
        timeout_text="timed out: the patch stays blocked",
    )


def question_approval(question: str) -> Approval:
    return Approval(
        id="question:0",
        kind=ApprovalKind.QUESTION,
        step=0,
        phase=Phase.UNDERSTAND,
        eyebrow="A QUESTION BEFORE THE AGENT STARTS",
        headline=question,
        free_text=FreeText(label=question, placeholder="Your answer"),
        layout=Layout.QUESTION,
        choices=[
            Choice(id="answer", label="Answer", approves=True, primary=True),
            Choice(id="stop", label="Stop the run", approves=False),
        ],
        on_timeout="stop",
        timeout_text="timed out: the run stops",
    )
