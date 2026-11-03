"""The shapes every policy layer shares: decisions, risks, rule hits and the final verdict."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class PolicyError(Exception):
    """The rulebook (or another policy file) is malformed. Raised at load time, loudly."""


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

    @property
    def strictness(self) -> int:
        return _STRICTNESS[self]


_STRICTNESS: Final = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RuleSource(StrEnum):
    """Where a matched rule came from. Plans and learned rules can only narrow."""

    RULEBOOK = "rulebook"
    PLAN = "plan"
    LEARNED = "learned"
    TAINT = "taint"


@dataclass(frozen=True)
class RuleHit:
    """One rule that matched an action."""

    rule_id: str
    source: RuleSource
    decision: Decision
    risk: Risk
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "source": self.source.value,
            "decision": self.decision.value,
            "risk": self.risk.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RuleSpec:
    """A rule's identity and verdict, without its matching logic."""

    rule_id: str
    decision: Decision
    risk: Risk
    reason: str

    def hit(self, source: RuleSource, detail: str | None = None) -> RuleHit:
        """A hit for this rule; ``detail`` (a full sentence) follows the reason when given."""
        reason = f"{self.reason} {detail}" if detail else self.reason
        return RuleHit(self.rule_id, source, self.decision, self.risk, reason)


def strictest(hits: Sequence[RuleHit]) -> RuleHit:
    """Deny beats ask beats allow; on a tie the earlier hit keeps the decision."""
    if not hits:
        raise ValueError("strictest() needs at least one rule hit")
    winner = hits[0]
    for hit in hits[1:]:
        if hit.decision.strictness > winner.decision.strictness:
            winner = hit
    return winner


@dataclass(frozen=True)
class PolicyDecision:
    """The verdict on one action, with everything the panel and the report show.

    ``rule_id``, ``reason`` and ``risk`` come from the winning rule; ``matched_rules`` lists
    every rule that matched, each with its source. ``source`` names the untrusted input
    suspected of steering the agent (only on tagged denials); ``taint_sources`` lists every
    untrusted input that was active when the decision was made.
    """

    decision: Decision
    rule_id: str
    reason: str
    risk: Risk
    matched_rules: tuple[RuleHit, ...]
    tags: tuple[str, ...] = ()
    source: str | None = None
    in_plan: bool = True
    taint_sources: tuple[str, ...] = field(default=())

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "risk": self.risk.value,
            "tags": list(self.tags),
            "source": self.source,
            "matched_rules": [hit.to_dict() for hit in self.matched_rules],
            "in_plan": self.in_plan,
            "taint_sources": list(self.taint_sources),
        }

    @classmethod
    def from_hits(cls, hits: Sequence[RuleHit]) -> PolicyDecision:
        winner = strictest(hits)
        return cls(
            decision=winner.decision,
            rule_id=winner.rule_id,
            reason=winner.reason,
            risk=winner.risk,
            matched_rules=tuple(hits),
            in_plan=not any(hit.source is RuleSource.PLAN for hit in hits),
        )
