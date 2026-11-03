"""Validate the shape of an action proposed by the agent (model output: untrusted)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Tool(StrEnum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    LIST_DIR = "list_dir"
    RUN_COMMAND = "run_command"
    HTTP_GET = "http_get"
    ROLLBACK = "rollback"
    FINISH = "finish"


PATH_TOOLS: Final = frozenset({Tool.READ_FILE, Tool.WRITE_FILE, Tool.LIST_DIR})
COMMAND_TOOLS: Final = frozenset({Tool.RUN_COMMAND})
FETCH_TOOLS: Final = frozenset({Tool.HTTP_GET})
ALL_TOOLS: Final = frozenset(Tool)


@dataclass(frozen=True)
class Action:
    """A well-formed action. ``tool`` may still be unknown: the rulebook denies those."""

    tool: str
    args: Mapping[str, object]

    @property
    def path(self) -> str:
        """The path argument of a path tool (``list_dir`` defaults to the workspace)."""
        value = self.args.get("path", ".")
        return value if isinstance(value, str) else ""

    @property
    def command(self) -> str:
        value = self.args.get("command", "")
        return value if isinstance(value, str) else ""

    @property
    def url(self) -> str:
        value = self.args.get("url", "")
        return value if isinstance(value, str) else ""

    @property
    def content(self) -> str:
        value = self.args.get("content", "")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class InvalidAction:
    """An action that failed validation, with a plain-English problem."""

    problem: str


# Required and optional arguments per known tool, with their types.
_REQUIRED: Final[dict[str, tuple[tuple[str, type], ...]]] = {
    Tool.READ_FILE: (("path", str),),
    Tool.WRITE_FILE: (("path", str), ("content", str)),
    Tool.LIST_DIR: (),
    Tool.RUN_COMMAND: (("command", str),),
    Tool.HTTP_GET: (("url", str),),
    Tool.ROLLBACK: (("checkpoint", int),),
    Tool.FINISH: (),
}
_OPTIONAL: Final[dict[str, tuple[tuple[str, type], ...]]] = {
    Tool.LIST_DIR: (("path", str),),
    Tool.HTTP_GET: (("question", str),),
    Tool.FINISH: (("summary", str),),
}


def parse_action(raw: object) -> Action | InvalidAction:
    """Check ``{"tool": str, "args": {...}}`` and the argument types of known tools."""
    if not isinstance(raw, Mapping):
        return InvalidAction("the action is not an object")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool:
        return InvalidAction("the action has no tool name")
    args = raw.get("args", {})
    if not isinstance(args, Mapping) or not all(isinstance(key, str) for key in args):
        return InvalidAction("the action's args are not an object")
    for name, expected in _REQUIRED.get(tool, ()):
        if name not in args:
            return InvalidAction(f"{tool} needs a '{name}' argument")
        if not _has_type(args[name], expected):
            return InvalidAction(f"{tool} needs '{name}' to be {_type_word(expected)}")
    for name, expected in _OPTIONAL.get(tool, ()):
        if name in args and not _has_type(args[name], expected):
            return InvalidAction(f"{tool} needs '{name}' to be {_type_word(expected)}")
    if tool == Tool.ROLLBACK and isinstance(args["checkpoint"], int) and args["checkpoint"] < 0:
        return InvalidAction("rollback needs a checkpoint number of 0 or more")
    return Action(tool=tool, args=dict(args))


def _has_type(value: object, expected: type) -> bool:
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def _type_word(expected: type) -> str:
    return "a whole number" if expected is int else "text"
