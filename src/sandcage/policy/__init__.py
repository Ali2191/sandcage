"""The policy gate: every action the agent proposes is allowed, denied or asked here.

Evaluate through a :class:`PolicySession` (one per run). The stateless
:meth:`PolicyEngine.evaluate` never applies the diff guard, taint or extra layers.
"""

from sandcage.policy.engine import PolicyEngine
from sandcage.policy.rulebook import DEFAULT_RULEBOOK_PATH, Rulebook, load_rulebook
from sandcage.policy.session import PolicyLockedError, PolicySession, RuleLayer, WorkspaceView
from sandcage.policy.types import (
    Decision,
    PolicyDecision,
    PolicyError,
    Risk,
    RuleHit,
    RuleSource,
    RuleSpec,
)

__all__ = [
    "DEFAULT_RULEBOOK_PATH",
    "Decision",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyError",
    "PolicyLockedError",
    "PolicySession",
    "Risk",
    "RuleHit",
    "RuleLayer",
    "RuleSource",
    "RuleSpec",
    "Rulebook",
    "WorkspaceView",
    "load_rulebook",
]
