"""The blast-radius report, built from the harness's own record, never from model text.

- **Trifecta** per step (:func:`trifecta`): *private data* once a result the agent saw had
  secrets masked out of it (it stays in the context); *untrusted content* when the step
  was tainted or read a flagged source; *a way out* when the step **executed** something
  that can send data out (a web fetch or a network command). The report states whether all
  three ever met, and at which steps.
- **Capabilities**: read / write / command / network / control, attempted vs executed vs
  blocked, with targets. "Blocked" = denied, or asked and not approved.
- **Egress** is measured from the fetcher's per-hop record (``run.egress``): host,
  resolved address, status and bytes received for every request actually made. A network
  attempt that was blocked appears with ``requests: 0``, because no request left.
- **Honest labels**: tokens are "estimated" unless the provider reported every count; the
  cost is shown only for reported usage; observe mode (the Bench baseline and guard-off)
  says the gate only recorded its verdicts; a scripted run says no real model was used.
- **Headline and outcome** follow PAGES.md section 5, plus the withheld case: a withheld
  patch never reads "Delivered" or "the task got done".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import JsonValue

from sandcage.contract.approval import RedteamFinding
from sandcage.contract.common import Phase, Plan
from sandcage.contract.events import (
    Budget,
    CapabilityCount,
    EgressRecord,
    ReportEvent,
    RunStatus,
    StepEvent,
    Trifecta,
)
from sandcage.contract.wording import target_hosts
from sandcage.masking import mask
from sandcage.sandbox.fetcher import Hop

INJECTION_TAG: Final = "prompt_injection_suspected"
NETWORK_TOOLS: Final = frozenset({"http_get"})
CAPABILITIES: Final = ("read", "write", "command", "network", "control")
OUTCOME_DELIVERED: Final = "Delivered"
OUTCOME_WITHHELD: Final = "Withheld"
OUTCOME_STOPPED: Final = "Stopped by you"
_UNSAFE_MD: Final = re.compile(r"[`<>\r\n\t]+")


# --- small wording helpers ----------------------------------------------------------------


def times(n: int) -> str:
    return "once" if n == 1 else f"{n} times"


def md_text(value: object, limit: int = 300) -> str:
    """Untrusted text for Markdown: masked, one line, no HTML or code fences, capped."""
    text = mask(str(value)) if value is not None else ""
    return _UNSAFE_MD.sub(" ", text).strip()[:limit]


def outcome(status: RunStatus, *, withheld: bool) -> str:
    """The solid outcome pill. A withheld patch is never "Delivered"."""
    if status is RunStatus.STOPPED:
        return OUTCOME_STOPPED
    if withheld:
        return OUTCOME_WITHHELD
    if status is RunStatus.FINISHED:
        return OUTCOME_DELIVERED
    return status.value


def headline(status: RunStatus, blocked: int, *, withheld: bool) -> str:
    """The report headline (PAGES.md section 5, plus the withheld case)."""
    if status is RunStatus.STOPPED:
        if blocked:
            return f"You stopped this run. Before that, Sandcage blocked it {times(blocked)}."
        return "You stopped this run. Nothing left the cage."
    if withheld:
        if blocked:
            return (
                f"The agent tried to cross the line. Sandcage blocked it {times(blocked)}, "
                "and the patch is withheld until you review it."
            )
        return "The run finished inside the cage, but the patch is withheld until you review it."
    if status is RunStatus.FINISHED:
        if blocked:
            return (
                f"The agent tried to cross the line. Sandcage blocked it {times(blocked)}, "
                "and the task still got done."
            )
        return "The run finished inside the cage, and the task got done."
    if blocked:
        return (
            f"Sandcage blocked the agent {times(blocked)}. The run ended before the task was done."
        )
    return "The run ended before the task was done."


# --- per-step classification --------------------------------------------------------------


def is_blocked(event: StepEvent) -> bool:
    return event.blocked and not event.observed


def is_network(event: StepEvent) -> bool:
    if event.tool in NETWORK_TOOLS:
        return True
    rules = {event.rule_id, *(rule.rule_id for rule in event.matched_rules)}
    return any(rule.startswith(("net.", "plan.network")) for rule in rules)


def capability(event: StepEvent) -> str:
    if is_network(event):
        return "network"
    if event.tool in {"read_file", "list_dir"}:
        return "read"
    if event.tool == "write_file":
        return "write"
    if event.tool == "run_command":
        return "command"
    return "control"


def target(event: StepEvent) -> str:
    args = event.args
    for key in ("command", "url", "path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return md_text(value, 200)
    return event.tool


@dataclass(frozen=True)
class StepLegs:
    step: int
    private_data: bool
    untrusted_content: bool
    way_out: bool


def trifecta(
    steps: Sequence[StepEvent], suspicious: Sequence[str] = ()
) -> tuple[Trifecta, list[StepLegs], dict[str, bool]]:
    """The three legs at every step, whether they ever met, and which were attempted."""
    flagged = set(suspicious)
    private = False
    legs: list[StepLegs] = []
    attempted = {"private_data": False, "untrusted_content": False, "way_out": False}
    for event in steps:
        if event.executed and event.masked_secrets:
            private = True
        read_source = event.tool == "read_file" and event.args.get("path") in flagged
        untrusted = bool(event.taint_sources) or read_source or INJECTION_TAG in event.tags
        way_out = event.executed and not event.observed and is_network(event)
        if event.observed and event.executed and is_network(event):
            way_out = True  # observe mode: the action really ran
        rules = {event.rule_id, *(rule.rule_id for rule in event.matched_rules)}
        if event.masked_secrets or any("secret" in rule for rule in rules):
            attempted["private_data"] = True
        if untrusted:
            attempted["untrusted_content"] = True
        if is_network(event):
            attempted["way_out"] = True
        legs.append(StepLegs(event.step, private, untrusted, way_out))
    status = Trifecta(
        private_data=any(leg.private_data for leg in legs),
        untrusted_content=any(leg.untrusted_content for leg in legs),
        way_out=any(leg.way_out for leg in legs),
        all_at_once=any(leg.private_data and leg.untrusted_content and leg.way_out for leg in legs),
    )
    return status, legs, attempted


def capabilities(steps: Sequence[StepEvent]) -> list[CapabilityCount]:
    counts = {name: [0, 0, 0] for name in CAPABILITIES}
    for event in steps:
        row = counts[capability(event)]
        row[0] += 1
        row[1] += int(event.executed)
        row[2] += int(is_blocked(event))
    return [
        CapabilityCount(capability=name, attempted=a, executed=e, blocked=b)
        for name, (a, e, b) in counts.items()
    ]


def egress(steps: Sequence[StepEvent], hops: Sequence[Hop]) -> list[EgressRecord]:
    """Every request actually made, then every network target that was only attempted."""
    records = [
        EgressRecord(
            host=md_text(hop.host, 200),
            address=hop.address,
            status=hop.status,
            bytes_received=hop.bytes_received,
            requests=1,
            blocked=False,
        )
        for hop in hops
    ]
    reached = {hop.host for hop in hops}
    seen: set[str] = set()
    for event in steps:
        if not is_network(event) or event.executed:
            continue
        for host in target_hosts(event.tool, event.args):
            if host not in reached and host not in seen:
                seen.add(host)
                records.append(
                    EgressRecord(
                        host=md_text(host, 200),
                        address=None,
                        status=None,
                        bytes_received=0,
                        requests=0,
                        blocked=True,
                    )
                )
    return records


# --- the report ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportInput:
    run_id: str
    repo: str
    brief: str
    status: RunStatus
    guard: bool
    observe: bool
    steps: Sequence[StepEvent]
    hops: Sequence[Hop] = ()
    model: str | None = None
    provider: str | None = None
    sandbox_runtime: str | None = None
    plan: Plan | None = None
    scout_flags: Sequence[str] = ()
    suspicious_pages: Sequence[str] = ()
    verified: bool | None = None
    findings: Sequence[RedteamFinding] = ()
    unreviewed: bool = False
    delivered_files: Sequence[str] = ()
    excluded: Sequence[tuple[str, str]] = ()
    withheld: bool = False
    released_by_human: bool = False
    budget: Budget | None = None
    tokens_by_role: dict[str, int] = field(default_factory=dict)
    summary: str = ""
    breaker_trips: int = 0
    checkpoints: int = 0
    rollbacks: int = 0


@dataclass(frozen=True)
class Report:
    data: dict[str, JsonValue]
    markdown: str
    event: ReportEvent


def build_report(source: ReportInput, ts: float) -> Report:
    steps = list(source.steps)
    blocks = [e for e in steps if is_blocked(e)]
    status, legs, attempted = trifecta(steps, source.scout_flags)
    caps = capabilities(steps)
    exits = egress(steps, source.hops)
    pill = outcome(source.status, withheld=source.withheld)
    line = headline(source.status, len(blocks), withheld=source.withheld)
    budget = source.budget
    reported = budget.tokens_reported if budget is not None else False
    data: dict[str, JsonValue] = {
        "run_id": source.run_id,
        "repo": source.repo,
        "task": md_text(source.brief, 2000),
        "status": source.status.value,
        "outcome": pill,
        "headline": line,
        "guard": source.guard,
        "mode": "observe" if source.observe else "enforce",
        "model": source.model,
        "provider": source.provider,
        "sandbox_runtime": source.sandbox_runtime,
        "summary": md_text(source.summary, 1000),
        "plan": source.plan.model_dump(mode="json") if source.plan else None,
        "scout_flags": list(source.scout_flags),
        "suspicious_pages": [md_text(p, 300) for p in source.suspicious_pages],
        "trifecta": status.model_dump(mode="json"),
        "trifecta_attempted": dict(attempted),
        "trifecta_steps": [
            {
                "step": leg.step,
                "private_data": leg.private_data,
                "untrusted_content": leg.untrusted_content,
                "way_out": leg.way_out,
            }
            for leg in legs
        ],
        "capabilities": [c.model_dump(mode="json") for c in caps],
        "blocks": [
            {
                "step": e.step,
                "tool": e.tool,
                "target": target(e),
                "decision": e.decision.value,
                "approved": e.approved,
                "rule_id": e.rule_id,
                "sentence": e.blocked_sentence,
                "matched_rules": [r.model_dump(mode="json") for r in e.matched_rules],
                "taint_sources": list(e.taint_sources),
                "tags": list(e.tags),
            }
            for e in blocks
        ],
        "egress": [r.model_dump(mode="json") for r in exits],
        "verified": source.verified,
        "redteam": {
            "findings": [f.model_dump(mode="json") for f in source.findings],
            "unreviewed": source.unreviewed,
        },
        "delivery": {
            "files": list(source.delivered_files),
            "excluded": [{"path": p, "reason": r} for p, r in source.excluded],
            "withheld": source.withheld,
            "released_by_human": source.released_by_human,
        },
        "delivery_blocked": source.withheld,
        "released_by_human": source.released_by_human,
        "breaker_trips": source.breaker_trips,
        "checkpoints": source.checkpoints,
        "rollbacks": source.rollbacks,
        "budget": budget.model_dump(mode="json") if budget else None,
        "tokens_reported": reported,
        "tokens_by_role": dict(sorted(source.tokens_by_role.items())),
    }
    event = ReportEvent(
        run_id=source.run_id,
        phase=Phase.REPORT,
        ts=ts,
        budget=budget,
        trifecta=status,
        blocks=len(blocks),
        capabilities=caps,
        outcome=pill,
        headline=line,
        egress=exits,
    )
    return Report(data, report_markdown(source, data, status, blocks, exits), event)


def report_markdown(
    source: ReportInput,
    data: dict[str, JsonValue],
    status: Trifecta,
    blocks: Sequence[StepEvent],
    exits: Sequence[EgressRecord],
) -> str:
    lines = [
        "# Sandcage run report",
        "",
        f"**{data['outcome']}.** {data['headline']}",
        "",
        f"- Run: `{source.run_id}` on {md_text(source.repo, 100)}",
        f"- Task: {md_text(source.brief, 500)}",
        f"- Status: {source.status.value}",
        f"- Model: {md_text(source.model, 100) if source.model else 'scripted (no real model)'}",
        f"- Sandbox: {md_text(source.sandbox_runtime, 100) or 'unknown'}",
    ]
    if source.observe:
        lines.append(
            "- Mode: **observe**. The gate only recorded what it would have decided; every "
            "action ran (the Bench baseline, or guard off)."
        )
    elif not source.guard:
        lines.append("- Guard: off (tool output was not wrapped as untrusted).")
    lines += ["", "## The dangerous combination", ""]
    lines.append(
        "All three were present at the same step."
        if status.all_at_once
        else "This run never had all three at once."
    )
    for name, present in (
        ("Private data", status.private_data),
        ("Untrusted content", status.untrusted_content),
        ("Talking to the outside", status.way_out),
    ):
        lines.append(f"- {name}: {'present' if present else 'blocked'}")
    lines += [
        "",
        "## What it tried",
        "",
        "| Capability | Attempted | Ran | Blocked |",
        "|---|---|---|---|",
    ]
    caps = data["capabilities"]
    assert isinstance(caps, list)  # noqa: S101 - built above
    for row in caps:
        assert isinstance(row, dict)  # noqa: S101
        lines.append(
            f"| {row['capability']} | {row['attempted']} | {row['executed']} | {row['blocked']} |"
        )
    lines += ["", f"## Blocked ({len(blocks)})", ""]
    for event in blocks:
        sentence = md_text(event.blocked_sentence or event.tool)
        lines.append(f"- Step {event.step}: {sentence} (rule {md_text(event.rule_id, 80)})")
    if not blocks:
        lines.append("- Nothing was blocked.")
    lines += ["", "## Egress (measured)", ""]
    if not exits:
        lines.append("- No network request was made or attempted. 0 bytes sent.")
    for record in exits:
        if record.blocked:
            lines.append(f"- {record.host}: blocked, 0 requests, 0 bytes sent")
        else:
            lines.append(
                f"- {record.host} ({record.address}): HTTP {record.status}, "
                f"{record.bytes_received} bytes received"
            )
    lines += _after_lines(source)
    return "\n".join(lines) + "\n"


def _after_lines(source: ReportInput) -> list[str]:
    """Checks, patch and cost sections of the Markdown report."""
    lines: list[str] = []
    lines += ["", "## Checks", ""]
    verified = (
        "passed" if source.verified else "not passed" if source.verified is False else "not run"
    )
    lines.append(f"- Independent check: {verified}")
    if source.unreviewed:
        lines.append("- Attacker review: the patch was **not reviewed**, so it is withheld.")
    else:
        lines.append(f"- Attacker review: {len(source.findings)} finding(s)")
    for finding in source.findings:
        where = md_text(finding.file, 200) + (f":{finding.line}" if finding.line else "")
        lines.append(
            f"  - {finding.severity.value}: {where or 'patch'}: {md_text(finding.explanation)}"
        )
    lines += ["", "## Patch", ""]
    if source.status is RunStatus.STOPPED:
        lines.append("- You stopped this run, so no patch was made.")
    elif source.withheld:
        lines.append("- **The patch is withheld.** A human must release it before download.")
    elif source.released_by_human:
        lines.append("- The patch was withheld, then released by you.")
    for path in source.delivered_files:
        lines.append(f"- {md_text(path, 200)}")
    for path, reason in source.excluded:
        lines.append(f"- left out: {md_text(path, 200)} ({md_text(reason, 100)})")
    lines += ["", "## Cost", ""]
    budget = source.budget
    if budget is None:
        lines.append("- No usage recorded.")
    else:
        label = "" if budget.tokens_reported else " (estimated: the provider did not report usage)"
        lines.append(f"- Tokens: {budget.tokens}{label}")
        lines.append(f"- Steps: {budget.steps} of {budget.max_steps}")
        lines.append(f"- Time: {budget.seconds:.0f} s (waiting for you not counted)")
    return lines
