"""The typed contract shared by the backend and the panel: every event, and the one
approval record every human question uses. Generated files live in ``contract/``."""

from sandcage.contract.approval import (
    ActionContext,
    Approval,
    ApprovalAnswerError,
    ApprovalKind,
    Choice,
    Resolution,
    action_approval,
    amendment_approval,
    breaker_approval,
    plan_approval,
    question_approval,
    redteam_approval,
)
from sandcage.contract.common import ClippedEntry, Manifest, MatchedRule, Phase, Plan
from sandcage.contract.events import (
    EVENT_KINDS,
    Budget,
    Event,
    EventParseError,
    RunStatus,
    UnknownEvent,
    dump_event,
    parse_event,
)

__all__ = [
    "EVENT_KINDS",
    "ActionContext",
    "Approval",
    "ApprovalAnswerError",
    "ApprovalKind",
    "Budget",
    "Choice",
    "ClippedEntry",
    "Event",
    "EventParseError",
    "Manifest",
    "MatchedRule",
    "Phase",
    "Plan",
    "Resolution",
    "RunStatus",
    "UnknownEvent",
    "action_approval",
    "amendment_approval",
    "breaker_approval",
    "dump_event",
    "parse_event",
    "plan_approval",
    "question_approval",
    "redteam_approval",
]
