"""Shared helpers for policy tests: one engine, action builders and a dict workspace."""

from __future__ import annotations

from functools import cache

from sandcage.policy import PolicyEngine, PolicySession, RuleLayer


@cache
def engine() -> PolicyEngine:
    return PolicyEngine.from_file()


class DictWorkspace:
    """A workspace view backed by a dict of relative path -> text."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)


def session(
    files: dict[str, str] | None = None,
    *,
    layers: tuple[RuleLayer, ...] = (),
    may_edit_tests: bool = False,
) -> PolicySession:
    return PolicySession(
        engine(), workspace=DictWorkspace(files), layers=layers, may_edit_tests=may_edit_tests
    )


def read(path: str) -> dict[str, object]:
    return {"tool": "read_file", "args": {"path": path}}


def write(path: str, content: str = "x = 1\n") -> dict[str, object]:
    return {"tool": "write_file", "args": {"path": path, "content": content}}


def ls(path: str | None = None) -> dict[str, object]:
    return {"tool": "list_dir", "args": {} if path is None else {"path": path}}


def run(command: str) -> dict[str, object]:
    return {"tool": "run_command", "args": {"command": command}}


def fetch(url: str) -> dict[str, object]:
    return {"tool": "http_get", "args": {"url": url, "question": "what is it?"}}


def rollback(checkpoint: object) -> dict[str, object]:
    return {"tool": "rollback", "args": {"checkpoint": checkpoint}}


def finish() -> dict[str, object]:
    return {"tool": "finish", "args": {"summary": "done"}}
