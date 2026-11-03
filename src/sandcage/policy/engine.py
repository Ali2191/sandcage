"""The stateless rulebook evaluator. Runs use :class:`PolicySession`, which adds the diff
guard, plan and learned layers, and taint tracking on top of this."""

from __future__ import annotations

from pathlib import Path

from sandcage.policy.actions import Action, InvalidAction, Tool, parse_action
from sandcage.policy.commands import parse_command
from sandcage.policy.matchers import Subject
from sandcage.policy.rulebook import (
    ACTION_INVALID,
    DEFAULT_DENY,
    DEFAULT_RULEBOOK_PATH,
    Rulebook,
    load_rulebook,
)
from sandcage.policy.types import PolicyDecision, RuleHit, RuleSource


class PolicyEngine:
    """Judges one action against the rulebook: the first matching rule wins, and an
    action no rule matches is denied."""

    def __init__(self, rulebook: Rulebook) -> None:
        self._rulebook = rulebook

    @classmethod
    def from_file(cls, path: Path = DEFAULT_RULEBOOK_PATH) -> PolicyEngine:
        return cls(load_rulebook(path))

    @property
    def rulebook(self) -> Rulebook:
        return self._rulebook

    def evaluate(self, raw: object) -> PolicyDecision:
        """Stateless decision for one proposed action (no taint, no diff guard)."""
        action = parse_action(raw)
        if isinstance(action, InvalidAction):
            return PolicyDecision.from_hits([invalid_hit(action)])
        return PolicyDecision.from_hits([self.rulebook_hit(self.subject(action))])

    def subject(self, action: Action) -> Subject:
        command = parse_command(action.command) if action.tool == Tool.RUN_COMMAND else None
        return Subject(action=action, command=command)

    def rulebook_hit(self, subject: Subject) -> RuleHit:
        ctx = self._rulebook.context
        for rule in self._rulebook.rules:
            if rule.matches(subject, ctx):
                return rule.spec.hit(RuleSource.RULEBOOK)
        return DEFAULT_DENY.hit(RuleSource.RULEBOOK)


def invalid_hit(action: InvalidAction) -> RuleHit:
    return ACTION_INVALID.hit(RuleSource.RULEBOOK, f"Problem: {action.problem}.")
