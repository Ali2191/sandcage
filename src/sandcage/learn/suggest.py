"""Rule suggestions from what happened in a run: deterministic, never a model.

- ``suspicious_file``: a file the scout flagged *for instructions* (not a fail-closed
  flag), or a workspace file that was the source of injection-tagged blocks;
- ``ask_host``: the host of a page the reader said contains instructions;
- ``deny_command``: a command a human refused when asked (skipped when it was masked).

Blocks the rulebook or the plan already handle become notes ("already covered"), not
rules; a suggestion that is already learned becomes a note too. Accepting one is a human
action (:func:`sandcage.learn.learned.accept`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sandcage.contract.events import LearnSuggestion, StepEvent
from sandcage.learn.learned import normalise_command, rule_id
from sandcage.plan.manifest import plain_host
from sandcage.policy import Decision
from sandcage.roles.scout import FLAG_INSTRUCTIONS

INJECTION_TAG = "prompt_injection_suspected"


def _suggestion(kind: str, target: str, explanation: str) -> LearnSuggestion:
    return LearnSuggestion(
        id=f"{kind}:{target}",
        kind=kind,
        rule_id=rule_id(kind, target),
        explanation=explanation,
        target=target,
    )


def suggest(
    steps: Sequence[StepEvent],
    *,
    repo: str,
    scout_flags: Iterable[tuple[str, str]] = (),
    suspicious_pages: Iterable[str] = (),
    learned_ids: Iterable[str] = (),
) -> tuple[list[LearnSuggestion], list[str]]:
    found: dict[str, LearnSuggestion] = {}
    for path, reason in scout_flags:
        if reason == FLAG_INSTRUCTIONS:
            found.setdefault(
                f"suspicious_file:{path}",
                _suggestion(
                    "suspicious_file",
                    path,
                    f"Treat {path} as suspicious in {repo}: it contains instructions for AI "
                    "agents, so the scout flags it without a model and reading it taints the run.",
                ),
            )
    for event in steps:
        source = event.source
        if INJECTION_TAG in event.tags and event.blocked and source and "://" not in source:
            found.setdefault(
                f"suspicious_file:{source}",
                _suggestion(
                    "suspicious_file",
                    source,
                    f"Treat {source} as suspicious in {repo}: blocked actions were traced to it.",
                ),
            )
    for url in suspicious_pages:
        host = plain_host(url)
        if host:
            found.setdefault(
                f"ask_host:{host}",
                _suggestion(
                    "ask_host",
                    host,
                    f"Ask before fetching from {host}: a page there contained instructions "
                    "for AI agents.",
                ),
            )
    for event in steps:
        command = event.args.get("command")
        refused = event.decision is Decision.ASK and event.approved is False
        if event.tool != "run_command" or not refused or not isinstance(command, str):
            continue
        if "[MASKED:" in command:
            continue
        exact = normalise_command(command)
        found.setdefault(
            f"deny_command:{exact}",
            _suggestion("deny_command", exact, f"Deny `{exact}`: a human refused it in this run."),
        )
    known = set(learned_ids)
    notes = [f"Already learned: {s.explanation}" for s in found.values() if s.rule_id in known]
    suggestions = [s for s in found.values() if s.rule_id not in known]
    covered: list[str] = []
    for event in steps:
        if event.decision is Decision.DENY and not event.observed and event.blocked_sentence:
            note = f"Already covered by {event.rule_id}: {event.blocked_sentence}."
            if note not in covered:
                covered.append(note)
    return suggestions, [*notes, *covered]
