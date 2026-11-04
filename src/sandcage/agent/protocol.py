"""The JSON action protocol: one ``{"tool", "args", "reason"}`` object per model turn.

Model output is untrusted input. Everything here is a pure function over text, and every
problem raises :class:`ActionParseError` with a message written for the model (it is fed
back on the one retry). Other roles (planner, verifier, red-team) reuse
:func:`extract_json_object` for their own JSON replies.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

TOOLS: Final = (
    "list_dir",
    "read_file",
    "write_file",
    "run_command",
    "http_get",
    "amend_plan",
    "rollback",
    "finish",
)
MAX_REPLY_CHARS: Final = 200_000
REASON_LIMIT: Final = 500


class ActionParseError(ValueError):
    """The reply is not a usable action; ``str()`` is the message for the model."""


@dataclass(frozen=True)
class AgentAction:
    tool: str
    args: dict[str, JsonValue]
    reason: str

    def to_raw(self) -> dict[str, JsonValue]:
        """The ``{"tool", "args"}`` shape the policy gate and the sandbox take."""
        return {"tool": self.tool, "args": self.args}


def _objects(text: str) -> Iterator[dict[str, JsonValue]]:
    """Every JSON object in ``text``, in order (markdown fences and prose are skipped)."""
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            yield value
            index = text.find("{", end)
        else:
            index = text.find("{", index + 1)


def extract_json_object(text: str) -> dict[str, JsonValue]:
    """The first JSON object in free text."""
    if len(text) > MAX_REPLY_CHARS:
        raise ActionParseError(f"The reply is longer than {MAX_REPLY_CHARS} characters.")
    for found in _objects(text):
        return found
    raise ActionParseError("The reply has no JSON object. Reply with exactly one JSON object.")


def last_action_object(text: str) -> dict[str, JsonValue] | None:
    """The last JSON object with a ``tool`` key: an action a reasoning model left in its
    reasoning text instead of its answer."""
    if len(text) > MAX_REPLY_CHARS:
        return None
    found = [obj for obj in _objects(text) if isinstance(obj.get("tool"), str)]
    return found[-1] if found else None


def parse_action(text: str) -> AgentAction:
    """Read one action from a model reply."""
    data = extract_json_object(text)
    tool = data.get("tool")
    if not isinstance(tool, str) or tool not in TOOLS:
        names = ", ".join(TOOLS)
        raise ActionParseError(f"'tool' must be one of: {names}.")
    args = data.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ActionParseError("'args' must be a JSON object.")
    reason = data.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        raise ActionParseError("'reason' must be a string.")
    return AgentAction(tool=tool, args=args, reason=reason[:REASON_LIMIT])
