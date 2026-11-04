"""Validate a plan's capability manifest and clip it to what the rulebook allows.

A manifest comes from a model (or a human edit), so it is untrusted input. Every entry
is cleaned (control characters stripped, 200 characters max, 30 entries per list,
duplicates dropped); anything the rulebook would refuse anyway is clipped and reported,
so a plan shown to a human never claims a capability it cannot have. Narrowing itself is
guaranteed by strictest-wins in the session, not by clipping.

Paths are workspace-relative globs with the gate's gitignore-like semantics: write a
subtree as ``dir/**`` or ``dir/``. ``*`` stays inside one path component, so
``dir/*.py`` covers only the ``.py`` files directly in ``dir``; a pattern without a
slash (``invoice_calc``) matches that name at any depth.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Final

from sandcage.contract.common import ClippedEntry, Manifest
from sandcage.policy import Decision, PolicyEngine
from sandcage.policy.commands import normalize_program

MAX_ENTRIES: Final = 30
MAX_TEXT: Final = 200
_CONTROL: Final = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")
_PROGRAM: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_HOST: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_LIST_FIELDS: Final = ("read_paths", "write_paths", "commands", "network")
WORKSPACE_PREFIX: Final = "/workspace"

_Check = Callable[[str], tuple[str, str | None]]


def clean(value: object, limit: int = MAX_TEXT) -> str:
    """Model text made safe to store and show: no control or direction characters,
    trimmed, capped."""
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).strip()[:limit]


def validate_manifest(raw: object, engine: PolicyEngine) -> tuple[Manifest, list[ClippedEntry]]:
    clipped: list[ClippedEntry] = []
    if not isinstance(raw, Mapping):
        clipped.append(ClippedEntry(field="manifest", value="", reason="not an object"))
        raw = {}
    checks: dict[str, _Check] = {
        "read_paths": lambda text: _path(text, engine),
        "write_paths": lambda text: _path(text, engine),
        "commands": lambda text: _command(text, engine),
        "network": lambda text: _host(text, engine),
    }
    values: dict[str, list[str]] = {}
    for name in _LIST_FIELDS:
        values[name] = _entries(name, raw.get(name, []), checks[name], clipped)
    may_edit_tests = raw.get("may_edit_tests", False)
    if not isinstance(may_edit_tests, bool):
        clipped.append(
            ClippedEntry(
                field="may_edit_tests", value=clean(str(may_edit_tests)), reason="not true or false"
            )
        )
        may_edit_tests = False
    manifest = Manifest(
        read_paths=values["read_paths"],
        write_paths=values["write_paths"],
        commands=values["commands"],
        network=values["network"],
        may_edit_tests=may_edit_tests,
    )
    return manifest, clipped


def _entries(
    name: str,
    raw: object,
    check: _Check,
    clipped: list[ClippedEntry],
) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        clipped.append(ClippedEntry(field=name, value=clean(str(raw)), reason="not a list"))
        return []
    kept: list[str] = []
    for item in raw:
        text = clean(item)
        if not isinstance(item, str) or not text:
            clipped.append(
                ClippedEntry(field=name, value=clean(str(item)), reason="empty or not text")
            )
            continue
        value, problem = check(text)
        if problem is not None:
            clipped.append(ClippedEntry(field=name, value=text, reason=problem))
        elif value in kept:
            continue
        elif len(kept) >= MAX_ENTRIES:
            clipped.append(
                ClippedEntry(field=name, value=text, reason=f"more than {MAX_ENTRIES} entries")
            )
        else:
            kept.append(value)
    return kept


def normalise_path(text: str) -> str:
    """Strip the ``/workspace`` prefix and leading ``./``; the workspace itself is ``**``."""
    if text == WORKSPACE_PREFIX or text == WORKSPACE_PREFIX + "/":
        return "**"
    if text.startswith(WORKSPACE_PREFIX + "/"):
        text = text[len(WORKSPACE_PREFIX) + 1 :]
    while text.startswith("./"):
        text = text[2:]
    return "**" if text in {"", "."} else text


def _path(text: str, engine: PolicyEngine) -> tuple[str, str | None]:
    path = normalise_path(text)
    if path.startswith(("/", "~")) or any(mark in path for mark in ("$", "`", "\\")):
        return path, "outside the workspace"
    if ".." in PurePosixPath(path).parts:
        return path, "outside the workspace"
    context = engine.rulebook.context
    literal = context.workspace.root / path
    if context.secrets.matches(context.workspace, literal) or context.secrets.glob_may_match(path):
        return path, "a secret file: never readable or writable"
    return path, None


def _command(text: str, engine: PolicyEngine) -> tuple[str, str | None]:
    if not _PROGRAM.fullmatch(text):
        return text, "not a plain program name"
    program = normalize_program(text)
    decision = engine.evaluate({"tool": "run_command", "args": {"command": program}})
    if decision.decision is Decision.DENY:
        return program, f"the rulebook blocks it ({decision.rule_id})"
    return program, None


def plain_host(text: str) -> str | None:
    """``https://pypi.org/simple`` → ``pypi.org``; None when it is not a plain host."""
    host = text.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if "@" in host:
        return None
    if ":" in host:
        host, port = host.rsplit(":", 1)
        if not port.isdigit():
            return None
    return host if _HOST.fullmatch(host) else None


def _host(text: str, engine: PolicyEngine) -> tuple[str, str | None]:
    host = plain_host(text)
    if host is None:
        return text, "not a plain host name"
    decision = engine.evaluate({"tool": "http_get", "args": {"url": f"https://{host}/"}})
    if decision.decision is not Decision.ALLOW:
        return host, f"not on the rulebook's network allowlist ({decision.rule_id})"
    return host, None


def merge(base: Manifest, added: Manifest) -> Manifest:
    """``base`` extended by ``added`` (entries kept in order, no duplicates)."""

    def union(left: list[str], right: list[str]) -> list[str]:
        return left + [item for item in right if item not in left]

    return Manifest(
        read_paths=union(base.read_paths, added.read_paths),
        write_paths=union(base.write_paths, added.write_paths),
        commands=union(base.commands, added.commands),
        network=union(base.network, added.network),
        may_edit_tests=base.may_edit_tests or added.may_edit_tests,
    )


def difference(added: Manifest, base: Manifest) -> Manifest:
    """What ``added`` holds that ``base`` does not."""
    return Manifest(
        read_paths=[p for p in added.read_paths if p not in base.read_paths],
        write_paths=[p for p in added.write_paths if p not in base.write_paths],
        commands=[c for c in added.commands if c not in base.commands],
        network=[h for h in added.network if h not in base.network],
        may_edit_tests=added.may_edit_tests and not base.may_edit_tests,
    )


def is_empty(manifest: Manifest) -> bool:
    return not (
        manifest.read_paths
        or manifest.write_paths
        or manifest.commands
        or manifest.network
        or manifest.may_edit_tests
    )
