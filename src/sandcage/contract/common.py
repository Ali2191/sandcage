"""Shapes shared by events and approvals: the contract's building blocks.

Every model parses tolerantly (unknown fields are ignored, so an older reader survives a
newer writer) and serialises exactly the fields declared here. Enums reuse the policy's
own values so the contract and the gate cannot drift apart.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sandcage.policy.types import Decision, Risk, RuleSource

__all__ = [
    "ClippedEntry",
    "ContractModel",
    "Decision",
    "JsonValue",
    "Manifest",
    "MatchedRule",
    "Phase",
    "Plan",
    "Risk",
    "RuleSource",
]


class ContractModel(BaseModel):
    """Base for every contract model: frozen, unknown input fields ignored."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        json_schema_serialization_defaults_required=True,
    )


class Phase(StrEnum):
    """Pipeline stage an event belongs to."""

    UNDERSTAND = "understand"
    SCOUT = "scout"
    PLAN = "plan"
    APPROVE = "approve"
    ACT = "act"
    VERIFY = "verify"
    REDTEAM = "redteam"
    DELIVER = "deliver"
    REPORT = "report"
    LEARN = "learn"


class MatchedRule(ContractModel):
    """One rule that matched an action, with the layer it came from."""

    rule_id: str
    source: RuleSource
    decision: Decision
    risk: Risk
    reason: str


class Manifest(ContractModel):
    """The capabilities a plan declares. Paths are workspace-relative globs (``dir/**``)."""

    read_paths: list[str] = Field(default_factory=list)
    write_paths: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    network: list[str] = Field(default_factory=list)
    may_edit_tests: bool = False


class ClippedEntry(ContractModel):
    """A manifest entry removed because the rulebook would refuse it anyway."""

    field: str
    value: str
    reason: str


class Plan(ContractModel):
    """A plan as shown to people: what the agent will do and what it may touch."""

    steps: list[str]
    done_criteria: list[str]
    manifest: Manifest
    risk: Risk
    risk_reasons: list[str] = Field(default_factory=list)
    planner_risk: Risk | None = None
    edited_by_human: bool = False
    clipped: list[ClippedEntry] = Field(default_factory=list)
