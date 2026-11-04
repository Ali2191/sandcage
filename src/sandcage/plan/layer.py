"""The locked plan as a policy layer: the run's effective policy is rulebook ∩ plan.

:class:`PlanLayer` adds out-of-plan hits (source ``plan``, ids ``plan.*``) to the
session's strictest-wins merge, so a plan can only narrow what the rulebook allows:

| id | decision | when |
| --- | --- | --- |
| plan.read_not_listed | allow (flagged) | a read outside the read and write paths |
| plan.write_not_listed | ask | a write outside the write paths |
| plan.test_edit_not_allowed | ask | a test file written when the plan doesn't allow it |
| plan.command_not_listed | ask | a program the plan doesn't list |
| plan.network_not_listed | deny | a network program and no listed host in the command, |
| | | or a fetch to an unlisted host |

Amendments extend the layer only after a human approves them; the additions are
validated and clipped exactly like a planner reply first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from sandcage.contract.common import ClippedEntry, Manifest, Risk
from sandcage.contract.wording import NETWORK_PROGRAMS
from sandcage.plan.manifest import difference, is_empty, merge, plain_host, validate_manifest
from sandcage.plan.risk import highest, manifest_risks
from sandcage.policy import Decision, PolicyEngine
from sandcage.policy.actions import Tool
from sandcage.policy.globs import matches_any
from sandcage.policy.matchers import MatchContext, Subject
from sandcage.policy.types import RuleHit, RuleSource, RuleSpec

READ_NOT_LISTED: Final = RuleSpec(
    "plan.read_not_listed",
    Decision.ALLOW,
    Risk.LOW,
    "The plan does not list this file; reading it is allowed but flagged.",
)
WRITE_NOT_LISTED: Final = RuleSpec(
    "plan.write_not_listed",
    Decision.ASK,
    Risk.MEDIUM,
    "The plan does not let the agent write this file, so a human must approve it.",
)
TEST_EDIT_NOT_ALLOWED: Final = RuleSpec(
    "plan.test_edit_not_allowed",
    Decision.ASK,
    Risk.MEDIUM,
    "The plan does not allow editing tests, so a human must approve it.",
)
COMMAND_NOT_LISTED: Final = RuleSpec(
    "plan.command_not_listed",
    Decision.ASK,
    Risk.MEDIUM,
    "The plan does not list this program, so a human must approve it.",
)
NETWORK_NOT_LISTED: Final = RuleSpec(
    "plan.network_not_listed",
    Decision.DENY,
    Risk.HIGH,
    "The plan does not allow reaching this host, so it is blocked.",
)
AMENDMENT: Final = RuleSpec(
    "plan.amendment",
    Decision.ASK,
    Risk.MEDIUM,
    "The agent asks to extend the locked plan, so a human must approve it.",
)
PLAN_RULES: Final = (
    READ_NOT_LISTED,
    WRITE_NOT_LISTED,
    TEST_EDIT_NOT_ALLOWED,
    COMMAND_NOT_LISTED,
    NETWORK_NOT_LISTED,
    AMENDMENT,
)


class PlanLayer:
    """The locked manifest, judged per action. Mutable only through :meth:`extend`
    (an approved amendment)."""

    def __init__(self, manifest: Manifest, context: MatchContext) -> None:
        self._manifest = manifest
        self._context = context

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def extend(self, added: Manifest) -> None:
        self._manifest = merge(self._manifest, added)

    def hits(self, subject: Subject) -> list[RuleHit]:
        action = subject.action
        if action.tool == Tool.READ_FILE:
            return self._read_hits(action.path)
        if action.tool == Tool.WRITE_FILE:
            return self._write_hits(action.path)
        if action.tool == Tool.RUN_COMMAND:
            return self._command_hits(subject)
        if action.tool == Tool.HTTP_GET:
            host = plain_host(action.url)
            if host is None or host not in self._manifest.network:
                return [NETWORK_NOT_LISTED.hit(RuleSource.PLAN, f"Host: {host or 'unknown'}.")]
        return []

    def _relative(self, path: str) -> str | None:
        workspace = self._context.workspace
        resolved = workspace.resolve(path)
        if resolved is None or not workspace.contains(resolved):
            return None  # the rulebook already judges paths outside the workspace
        return workspace.relative(resolved)

    def _read_hits(self, path: str) -> list[RuleHit]:
        relative = self._relative(path)
        readable = (*self._manifest.read_paths, *self._manifest.write_paths)
        if relative is None or relative == "." or matches_any(readable, relative):
            return []
        return [READ_NOT_LISTED.hit(RuleSource.PLAN)]

    def _write_hits(self, path: str) -> list[RuleHit]:
        relative = self._relative(path)
        if relative is None:
            return []
        hits: list[RuleHit] = []
        if not matches_any(tuple(self._manifest.write_paths), relative):
            hits.append(WRITE_NOT_LISTED.hit(RuleSource.PLAN))
        if not self._manifest.may_edit_tests and matches_any(self._context.test_files, relative):
            hits.append(TEST_EDIT_NOT_ALLOWED.hit(RuleSource.PLAN))
        return hits

    def _command_hits(self, subject: Subject) -> list[RuleHit]:
        command = subject.command
        if command is None:
            return []
        hits: list[RuleHit] = []
        programs = command.programs
        unlisted = sorted(p for p in programs if p and p not in self._manifest.commands)
        if unlisted:
            hits.append(
                COMMAND_NOT_LISTED.hit(RuleSource.PLAN, f"Not listed: {', '.join(unlisted)}.")
            )
        if programs & NETWORK_PROGRAMS and not any(
            host in command.raw.lower() for host in self._manifest.network
        ):
            hits.append(NETWORK_NOT_LISTED.hit(RuleSource.PLAN))
        return hits


@dataclass(frozen=True)
class Amendment:
    """What an ``amend_plan`` request would add, after validation against the rulebook
    and minus what the plan already holds."""

    added: Manifest
    clipped: list[ClippedEntry]
    risk: Risk
    reasons: list[str]

    @property
    def empty(self) -> bool:
        """Nothing allowed is left: refused without asking a human."""
        return is_empty(self.added)


def propose_amendment(
    args: Mapping[str, JsonValue],
    current: Manifest,
    engine: PolicyEngine,
    suspicious: Iterable[str] = (),
) -> Amendment:
    validated, clipped = validate_manifest(dict(args), engine)
    added = difference(validated, current)
    found = manifest_risks(added, suspicious)
    risk = highest([Risk.MEDIUM, *(level for level, _ in found)])
    return Amendment(added, clipped, risk, [why for _, why in found])
