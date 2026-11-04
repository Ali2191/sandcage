"""The learned policy layer: rules a human accepted from a run's suggestions.

A learned rule can only **narrow** the policy (strictest wins):

- ``deny_command``: deny exactly this command (whitespace-normalised);
- ``ask_host``: ask a human before fetching from this host;
- ``suspicious_file``: this path (matched literally, never as a glob) is flagged by the
  scout without a model call, and reading it taints the run.

Rules are per repo. The file is YAML with a header; it is validated on load and before
every write, written atomically (temp file + rename), and a duplicate id is refused.
Nothing is ever added silently: only :func:`accept` writes, on a human's request.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import yaml

from sandcage.contract.events import LearnSuggestion
from sandcage.plan.manifest import plain_host
from sandcage.policy import Decision, Risk, RuleSource
from sandcage.policy.actions import Tool
from sandcage.policy.matchers import Subject
from sandcage.policy.types import RuleHit

Kind = Literal["suspicious_file", "ask_host", "deny_command"]
KINDS: Final = ("suspicious_file", "ask_host", "deny_command")
PREFIX: Final = "learned."
TARGET_LIMIT: Final = 300
HEADER: Final = (
    "# Sandcage learned rules. Each was accepted by a human from a run's suggestions.\n"
    "# They can only make the policy stricter. Edit with care; the file is validated.\n"
)


class LearnedRuleError(ValueError):
    """The learned file or a rule in it is invalid. The message is safe to show."""


class LearnedConflictError(LearnedRuleError):
    """A rule with this id is already learned."""


@dataclass(frozen=True)
class LearnedRule:
    id: str
    kind: Kind
    target: str
    repos: tuple[str, ...] = ()  # empty: every repo

    def applies_to(self, repo: str) -> bool:
        return not self.repos or repo in self.repos

    def to_yaml(self) -> dict[str, object]:
        return {"id": self.id, "kind": self.kind, "target": self.target, "repos": list(self.repos)}


def rule_id(kind: str, target: str) -> str:
    digest = hashlib.sha256(f"{kind}\x00{target}".encode()).hexdigest()[:10]
    return f"{PREFIX}{kind}.{digest}"


def normalise_command(command: str) -> str:
    return " ".join(command.split())


def rule_from(suggestion: LearnSuggestion, repo: str) -> LearnedRule:
    return validate_rule(
        {
            "id": suggestion.rule_id,
            "kind": suggestion.kind,
            "target": suggestion.target,
            "repos": [repo],
        }
    )


def validate_rule(raw: object, *, reserved: Iterable[str] = ()) -> LearnedRule:
    if not isinstance(raw, dict):
        raise LearnedRuleError("a learned rule must be a mapping")
    unknown = set(raw) - {"id", "kind", "target", "repos"}
    if unknown:
        raise LearnedRuleError(f"unknown keys in a learned rule: {sorted(unknown)}")
    rid, kind, target, repos = raw.get("id"), raw.get("kind"), raw.get("target"), raw.get("repos")
    if not isinstance(rid, str) or not rid.startswith(PREFIX) or len(rid) > 120:
        raise LearnedRuleError("a learned rule id must start with 'learned.'")
    if rid in set(reserved):
        raise LearnedRuleError(f"{rid} is already a rule id")
    if kind not in KINDS:
        raise LearnedRuleError(f"{rid}: kind must be one of {', '.join(KINDS)}")
    if not isinstance(target, str) or not target.strip() or len(target) > TARGET_LIMIT:
        raise LearnedRuleError(f"{rid}: target must be a short non-empty string")
    if "[MASKED:" in target:
        raise LearnedRuleError(f"{rid}: a masked value cannot be a rule target")
    if kind == "suspicious_file" and (target.startswith("/") or ".." in target.split("/")):
        raise LearnedRuleError(f"{rid}: a suspicious file must be a path inside the repo")
    if kind == "ask_host" and plain_host(f"https://{target}/") != target:
        raise LearnedRuleError(f"{rid}: ask_host needs a plain host name")
    if repos is None:
        repos = []
    if not isinstance(repos, list) or not all(isinstance(r, str) and r for r in repos):
        raise LearnedRuleError(f"{rid}: repos must be a list of repo names")
    clean_target = normalise_command(target) if kind == "deny_command" else target.strip()
    return LearnedRule(rid, kind, clean_target, tuple(repos))


def load_rules(path: Path, *, reserved: Iterable[str] = ()) -> list[LearnedRule]:
    """Every learned rule in the file (none when it does not exist)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise LearnedRuleError("the learned rules file is not valid YAML") from None
    if data is None:
        return []
    if not isinstance(data, dict) or set(data) != {"rules"} or not isinstance(data["rules"], list):
        raise LearnedRuleError("the learned rules file must hold one 'rules' list")
    rules: list[LearnedRule] = []
    seen = set(reserved)
    for raw in data["rules"]:
        rule = validate_rule(raw, reserved=seen)
        seen.add(rule.id)
        rules.append(rule)
    return rules


def accept(
    path: Path, suggestion: LearnSuggestion, repo: str, *, reserved: Iterable[str] = ()
) -> LearnedRule:
    """Append one accepted suggestion (validated, atomically). A duplicate id is refused."""
    existing = load_rules(path, reserved=reserved)
    rule = rule_from(suggestion, repo)
    if any(r.id == rule.id for r in existing):
        raise LearnedConflictError(f"{rule.id} is already learned")
    validate_rule(rule.to_yaml(), reserved=reserved)
    body = yaml.safe_dump(
        {"rules": [r.to_yaml() for r in [*existing, rule]]}, sort_keys=False, allow_unicode=True
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".learned-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(HEADER + body)
        Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return rule


# --- the layer -------------------------------------------------------------------------------


_DENY_REASON: Final = "A human refused this exact command before, so it is denied."
_ASK_REASON: Final = "A page from this host contained instructions for AI agents before."


class LearnedLayer:
    """The learned rules for one repo, as a policy layer (``SandboxRun(layers=...)``)."""

    def __init__(self, rules: Sequence[LearnedRule], repo: str) -> None:
        mine = [rule for rule in rules if rule.applies_to(repo)]
        self._deny = {rule.target: rule for rule in mine if rule.kind == "deny_command"}
        self._ask = {rule.target: rule for rule in mine if rule.kind == "ask_host"}
        self.suspicious = tuple(rule.target for rule in mine if rule.kind == "suspicious_file")
        self.ids = tuple(rule.id for rule in mine)

    def hits(self, subject: Subject) -> list[RuleHit]:
        action = subject.action
        if action.tool == Tool.RUN_COMMAND:
            rule = self._deny.get(normalise_command(action.command))
            if rule is not None:
                return [
                    RuleHit(rule.id, RuleSource.LEARNED, Decision.DENY, Risk.HIGH, _DENY_REASON)
                ]
        if action.tool == Tool.HTTP_GET:
            host = plain_host(action.url)
            rule = self._ask.get(host) if host else None
            if rule is not None:
                return [RuleHit(rule.id, RuleSource.LEARNED, Decision.ASK, Risk.HIGH, _ASK_REASON)]
        return []
