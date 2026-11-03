"""The per-run policy session: the rulebook plus every layer that needs run state.

For each action, in order:

1. validate its shape (``action.invalid``);
2. the rulebook decides (first match wins, else ``default.deny``);
3. extra layers (the locked plan, learned rules) add their hits;
4. the diff guard judges the content of a write that is not already denied;
5. strictest wins across all hits (deny over ask over allow; earlier hit on a tie);
6. taint: while untrusted content read recently is in play, a risky action that would be
   allowed becomes ``ask`` (``taint.escalate``), and every denial is tagged as a suspected
   prompt injection naming the source.

Taint: an allowed (or asked) read of an untrusted source taints the run for the next
``window`` actions. Each source has its own window; reading it again restarts it; every
action uses up one step of every window, whatever its decision. A denied read never
reached the agent, so it taints nothing. A source marked suspicious (flagged by the scout
or the web reader) taints for the rest of the run once read.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Protocol

from sandcage.masking import mask
from sandcage.policy.actions import InvalidAction, Tool, parse_action
from sandcage.policy.diff_guard import check_write
from sandcage.policy.engine import PolicyEngine, invalid_hit
from sandcage.policy.matchers import Subject
from sandcage.policy.rulebook import TAINT_ESCALATE
from sandcage.policy.types import Decision, PolicyDecision, RuleHit, RuleSource, strictest


class WorkspaceView(Protocol):
    """Read access to the run's workspace, for the diff guard and "is this a new file"."""

    def read_text(self, path: str) -> str | None:
        """Contents of the workspace-relative ``path``, or None when there is no such file."""
        ...


class RuleLayer(Protocol):
    """A source of extra rule hits (the locked plan, learned rules). Its hits are merged
    with strictest wins, so a layer can only make a decision stricter."""

    def hits(self, subject: Subject) -> Sequence[RuleHit]: ...


class PolicySession:
    def __init__(
        self,
        engine: PolicyEngine,
        *,
        workspace: WorkspaceView | None = None,
        layers: Sequence[RuleLayer] = (),
        may_edit_tests: bool = False,
    ) -> None:
        self._engine = engine
        self._workspace = workspace
        self._layers = tuple(layers)
        self._may_edit_tests = may_edit_tests
        self._windows: dict[str, int] = {}
        self._sticky: set[str] = set()
        self._suspicious: set[str] = set()
        self._last_read: dict[str, int] = {}
        self._step = 0
        self._trail: list[PolicyDecision] = []

    @property
    def trail(self) -> tuple[PolicyDecision, ...]:
        """Every decision this session made, in order."""
        return tuple(self._trail)

    @property
    def taint_sources(self) -> tuple[str, ...]:
        """Untrusted sources tainting the run right now, oldest read first."""
        active = set(self._windows) | self._sticky
        return tuple(sorted(active, key=lambda source: self._last_read.get(source, 0)))

    def mark_suspicious(self, source: str) -> None:
        """Flag a file or URL as carrying instructions: once read, it taints the rest of
        the run. A source already tainting the run becomes sticky now."""
        label = self._label(source)
        self._suspicious.add(label)
        if label in self._windows:
            del self._windows[label]
            self._sticky.add(label)

    def evaluate(self, raw: object) -> PolicyDecision:
        active = self.taint_sources
        action = parse_action(raw)
        subject: Subject | None = None
        if isinstance(action, InvalidAction):
            hits = [invalid_hit(action)]
        else:
            subject = self._engine.subject(action)
            hits = self._hits(subject)
            if active and strictest(hits).decision is Decision.ALLOW and self._risky(subject):
                hits.append(
                    TAINT_ESCALATE.hit(RuleSource.TAINT, f"Recently read: {', '.join(active)}.")
                )
        decision = PolicyDecision.from_hits(hits)
        if active:
            decision = self._with_taint(decision, active)
        self._advance(subject, decision.decision)
        self._trail.append(decision)
        return decision

    # --- decision ------------------------------------------------------------------

    def _hits(self, subject: Subject) -> list[RuleHit]:
        hits = [self._engine.rulebook_hit(subject)]
        for layer in self._layers:
            hits.extend(layer.hits(subject))
        if subject.action.tool == Tool.WRITE_FILE and strictest(hits).decision is not Decision.DENY:
            hits.extend(self._diff_guard(subject))
        return hits

    def _diff_guard(self, subject: Subject) -> list[RuleHit]:
        relative = self._inside(subject.action.path)
        if relative is None:
            return []
        rulebook = self._engine.rulebook
        return check_write(
            rulebook.diff_guard,
            rulebook.context.test_files,
            relative,
            self._read(relative),
            subject.action.content,
            may_edit_tests=self._may_edit_tests,
        )

    def _with_taint(self, decision: PolicyDecision, active: tuple[str, ...]) -> PolicyDecision:
        if decision.decision is not Decision.DENY:
            return dataclasses.replace(decision, taint_sources=active)
        note = (
            f" Untrusted content was read just before this ({', '.join(active)}), "
            "so this may be a prompt injection."
        )
        return dataclasses.replace(
            decision,
            taint_sources=active,
            tags=(*decision.tags, self._engine.rulebook.taint.tag),
            source=active[-1],
            reason=decision.reason + note,
        )

    def _risky(self, subject: Subject) -> bool:
        """Would this allowed action be dangerous if untrusted content is steering it?"""
        action = subject.action
        if action.tool == Tool.WRITE_FILE:
            relative = self._inside(action.path)
            return relative is None or self._read(relative) is None  # creating a file
        if action.tool != Tool.RUN_COMMAND or subject.command is None:
            return False
        command = subject.command
        if command.error is not None:
            return True
        ctx = self._engine.rulebook.context
        known = self._engine.rulebook.taint.known_commands
        for segment in command.segments:
            if segment.possible_programs & ctx.delete_programs or segment.wrappers:
                return True
            if any(r.writes and r.target not in ctx.safe_paths for r in segment.redirects):
                return True
            if not any(segment.words[: len(prefix)] == prefix for prefix in known):
                return True
        return False

    # --- taint state ---------------------------------------------------------------

    def _advance(self, subject: Subject | None, decision: Decision) -> None:
        self._step += 1
        for source in list(self._windows):
            self._windows[source] -= 1
            if self._windows[source] <= 0:
                del self._windows[source]
        if subject is None or decision is Decision.DENY:
            return
        for label in self._sources_read(subject):
            self._last_read[label] = self._step
            if label in self._suspicious:
                self._windows.pop(label, None)
                self._sticky.add(label)
            elif label not in self._sticky:
                self._windows[label] = self._engine.rulebook.taint.window

    def _sources_read(self, subject: Subject) -> list[str]:
        action = subject.action
        taint = self._engine.rulebook.taint
        if action.tool not in taint.untrusted_tools and action.tool != Tool.RUN_COMMAND:
            return []
        if action.tool == Tool.READ_FILE:
            return [self._label(action.path)]
        if action.tool == Tool.HTTP_GET:
            return [mask(action.url)]
        sources: list[str] = []
        for segment in subject.command.segments if subject.command else ():
            if segment.program in taint.reader_programs:
                for arg in segment.args:
                    relative = None if arg.startswith("-") else self._inside(arg)
                    if relative is not None and relative not in sources:
                        sources.append(mask(relative))
        return sources

    # --- helpers -------------------------------------------------------------------

    def _inside(self, path: str) -> str | None:
        workspace = self._engine.rulebook.context.workspace
        resolved = workspace.resolve(path)
        if resolved is None or not workspace.contains(resolved):
            return None
        return workspace.relative(resolved)

    def _label(self, source: str) -> str:
        if "://" in source:
            return mask(source)
        return mask(self._engine.rulebook.context.workspace.label(source))

    def _read(self, relative: str) -> str | None:
        return None if self._workspace is None else self._workspace.read_text(relative)
