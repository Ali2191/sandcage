"""Sandcage's own sentence for a blocked action ("Tried to send your .env to ...").

Written from the action and the rule that decided it, never from the agent's own label
(an attacker could write that). Patterns are the catalogue in Design/PAGES.md section 4.
The breaker banner uses the same phrases without "Tried to".
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Final
from urllib.parse import urlsplit

from sandcage.policy.commands import parse_command
from sandcage.policy.matchers import MatchContext

SecretCheck = Callable[[str], bool]

NETWORK_PROGRAMS: Final = frozenset(
    {
        "curl", "wget", "nc", "ncat", "netcat", "socat", "telnet", "ssh", "scp",
        "sftp", "ftp", "tftp", "rsync", "aria2c", "lftp",
    }
)  # fmt: skip
_SHELLS: Final = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_DELETE: Final = frozenset({"rm", "unlink", "rmdir", "shred"})
_URL: Final = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"]+", re.IGNORECASE)
_HOSTLIKE: Final = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?$", re.IGNORECASE)


def secret_check(context: MatchContext) -> SecretCheck:
    """The rulebook's own "is this a secret file" test, for workspace-relative paths."""

    def check(path: str) -> bool:
        resolved = context.workspace.resolve(path)
        return resolved is not None and context.secrets.matches(context.workspace, resolved)

    return check


def blocked_sentence(
    tool: str, args: Mapping[str, object], rule_id: str, *, is_secret: SecretCheck
) -> str:
    """The full sentence: "Tried to read your secrets in .env"."""
    return "Tried to " + blocked_phrase(tool, args, rule_id, is_secret=is_secret)


def blocked_phrase(
    tool: str, args: Mapping[str, object], rule_id: str, *, is_secret: SecretCheck
) -> str:
    """The sentence without "Tried to": "read your secrets in .env"."""
    if tool == "run_command":
        return _command_phrase(_text(args, "command"), rule_id, is_secret)
    path = _text(args, "path") or "."
    if tool == "read_file":
        if rule_id == "path.deny_outside_workspace":
            return f"read {path}, outside the repo"
        if is_secret(path):
            return f"read your secrets in {path}"
        return f"read {path}, which the plan doesn't cover"
    if tool == "write_file":
        return _write_phrase(path, rule_id, is_secret)
    if tool == "list_dir":
        return f"look inside {path}"
    if tool == "http_get":
        return f"fetch {_host(_text(args, 'url')) or 'a web page'}"
    if tool == "amend_plan":
        return "extend the locked plan"
    return f"use {tool}"


def _write_phrase(path: str, rule_id: str, is_secret: SecretCheck) -> str:
    by_rule = {
        "diff.deny_secrets": f"write a secret into {path}",
        "diff.ask_network_exec": f"add network code to {path}",
        "diff.deny_weakened_tests": f"weaken the tests in {path}",
        "ask.edit_tests": f"edit the tests in {path}",
        "plan.test_edit_not_allowed": f"edit the tests in {path}",
        "path.deny_outside_workspace": f"write {path}, outside the repo",
        "plan.write_not_listed": f"write {path}, outside the plan",
    }
    if rule_id in by_rule:
        return by_rule[rule_id]
    if is_secret(path):
        return f"overwrite your secrets in {path}"
    return f"write {path}"


def _command_phrase(command: str, rule_id: str, is_secret: SecretCheck) -> str:
    parsed = parse_command(command)
    programs = parsed.programs
    words = [word for segment in parsed.segments for word in segment.words]
    hosts = [host for host in (_host(word) for word in words) if host]
    secret = next(
        (w.lstrip("@<") for w in words if w.lstrip("@<") and is_secret(w.lstrip("@<"))), None
    )
    if programs & NETWORK_PROGRAMS:
        host = hosts[0] if hosts else None
        if secret and host:
            return f"send your {secret} to {host}"
        if programs & _SHELLS and host:
            return f"download a script from {host} and run it"
        return f"reach {host}" if host else "reach the network"
    if secret:
        return f"read your secrets in {secret}"
    first = parsed.segments[0] if parsed.segments else None
    program = first.program if first and first.program else command.split(" ", 1)[0]
    if programs & _DELETE:
        targets = [w for s in parsed.segments if s.program in _DELETE for w in s.args]
        shown = " ".join(t for t in targets if not t.startswith("-")) or "files"
        return f"delete {shown}"
    if rule_id == "cmd.deny_package_install":
        packages = [w for w in (first.args if first else ()) if not w.startswith("-")]
        packages = [w for w in packages if w not in {"install", "add", "i", "-r"}]
        return f"install {' '.join(packages) or 'packages'}"
    if rule_id == "plan.command_not_listed":
        return f"run {program}, which the plan doesn't allow"
    return f"run {program}" if program else "run a command"


def target_hosts(tool: str, args: Mapping[str, object]) -> list[str]:
    """Hosts an action reaches for (a fetched URL, or hosts in a command's words), in order."""
    if tool == "http_get":
        host = _host(_text(args, "url"))
        return [host] if host else []
    if tool != "run_command":
        return []
    parsed = parse_command(_text(args, "command"))
    words = [word for segment in parsed.segments for word in segment.words]
    found: list[str] = []
    for host in (_host(word) for word in words):
        if host and host not in found:
            found.append(host)
    return found


def _host(word: str) -> str | None:
    match = _URL.search(word)
    if match:
        try:
            host = urlsplit(match.group(0)).hostname
        except ValueError:
            return None
        return host or None
    if _HOSTLIKE.match(word) and not word.lower().endswith((".py", ".txt", ".md", ".env")):
        return word.split(":", 1)[0].lower()
    return None


def _text(args: Mapping[str, object], key: str) -> str:
    value = args.get(key, "")
    return value if isinstance(value, str) else ""
