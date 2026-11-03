"""The final diff between the original repo and a run's workspace.

This module sits below the harness, the verifier and the red-team in the import graph
(it imports only :mod:`safefs` and :mod:`masking`), so none of them can form a cycle.

- Files are compared through :mod:`safefs` (links are never followed and never diffed).
- The patch is git-style unified diff text, masked per file, capped at
  :data:`PATCH_LIMIT` characters with a marker.
- Secret files are never diffed: a changed secret file shows only a comment line saying its
  contents are hidden. A secret the run left out of the copy is not a deletion.
- Binary or undecodable files show a one-line comment instead of content.
- ``secrets`` lists, per file, the kinds of secret found in the *added* lines before
  masking (``secret_file`` for a changed secret file). Emails are masked in the patch but
  are not counted as secrets.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from sandcage.masking import EMAIL, PLACEHOLDER_PATTERN, mask
from sandcage.sandbox import safefs

PATCH_LIMIT: Final = 100_000
FILE_LIMIT: Final = 1_000_000
SECRET_FILE: Final = "secret_file"  # noqa: S105 - a kind name
TRUNCATION_MARKER: Final = f"\n[patch truncated at {PATCH_LIMIT} characters]\n"


class ChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class FileChange:
    path: str
    kind: ChangeKind
    added_lines: int = 0
    removed_lines: int = 0
    secret: bool = False
    binary: bool = False


@dataclass(frozen=True)
class WorkspaceDiff:
    files: tuple[FileChange, ...]
    patch: str
    secrets: Mapping[str, tuple[str, ...]]
    truncated: bool

    @property
    def empty(self) -> bool:
        return not self.files


@dataclass(frozen=True)
class _Content:
    """A file's bytes (possibly cut at FILE_LIMIT) and, when cut, its full digest."""

    data: bytes
    digest: str | None

    @property
    def text(self) -> str | None:
        if self.digest is not None:
            return None
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError:
            return None


def workspace_diff(
    original: Path,
    workspace: Path,
    *,
    is_secret: Callable[[str], bool],
    left_out: Iterable[str] = (),
) -> WorkspaceDiff:
    """Everything the run changed, as file entries, a masked patch and found secrets."""
    left = tuple(left_out)
    before = set(safefs.walk(original))
    after = set(safefs.walk(workspace))
    files: list[FileChange] = []
    sections: list[str] = []
    secrets: dict[str, tuple[str, ...]] = {}
    for path in sorted(before | after):
        old = _read(original, path) if path in before and not _under(path, left) else None
        new = _read(workspace, path) if path in after else None
        if (old is None and new is None) or not _differ(old, new):
            continue
        kind = (
            ChangeKind.ADDED
            if old is None
            else ChangeKind.DELETED
            if new is None
            else ChangeKind.MODIFIED
        )
        if is_secret(path):
            files.append(FileChange(path, kind, secret=True))
            sections.append(
                f"# {path}: secret file {kind.value}, contents hidden and not part of this patch\n"
            )
            secrets[path] = (SECRET_FILE,)
            continue
        old_text = "" if old is None else old.text
        new_text = "" if new is None else new.text
        if old_text is None or new_text is None:
            files.append(FileChange(path, kind, binary=True))
            sections.append(f"# {path}: binary or very large file {kind.value}, not shown\n")
            continue
        section, added, removed, added_text = _section(path, kind, old_text, new_text)
        files.append(FileChange(path, kind, added_lines=added, removed_lines=removed))
        sections.append(mask(section, source=path))
        found = _secret_kinds(added_text, path)
        if found:
            secrets[path] = found
    patch = "".join(sections)
    truncated = len(patch) > PATCH_LIMIT
    if truncated:
        patch = patch[:PATCH_LIMIT] + TRUNCATION_MARKER
    return WorkspaceDiff(tuple(files), patch, secrets, truncated)


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _read(root: Path, path: str) -> _Content | None:
    """None when the file vanished or became unsafe since the walk (never followed)."""
    try:
        data, cut = safefs.read_bytes(root, path, limit=FILE_LIMIT)
        return _Content(data, safefs.sha256(root, path) if cut else None)
    except (safefs.UnsafePathError, safefs.FileRefusedError):
        return None


def _differ(old: _Content | None, new: _Content | None) -> bool:
    if old is None or new is None:
        return True
    return old.digest != new.digest or old.data != new.data


def _section(path: str, kind: ChangeKind, old: str, new: str) -> tuple[str, int, int, str]:
    """Git-style diff text for one file, its added and removed line counts, and the raw
    text of the added lines."""
    header = [f"diff --git a/{path} b/{path}\n"]
    if kind is ChangeKind.ADDED:
        header.append("new file mode 100644\n")
    elif kind is ChangeKind.DELETED:
        header.append("deleted file mode 100644\n")
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="/dev/null" if kind is ChangeKind.ADDED else f"a/{path}",
            tofile="/dev/null" if kind is ChangeKind.DELETED else f"b/{path}",
        )
    )
    body: list[str] = []
    added: list[str] = []
    removed = 0
    for line in lines:
        is_content = not line.startswith(("---", "+++", "@@"))
        if is_content and line.startswith("+"):
            added.append(line[1:])
        elif is_content and line.startswith("-"):
            removed += 1
        body.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(header + body), len(added), removed, "".join(added)


def _secret_kinds(added_text: str, path: str) -> tuple[str, ...]:
    already = set(PLACEHOLDER_PATTERN.findall(added_text))
    masked = mask(added_text, source=path)
    kinds = {
        token[len("[MASKED:") : -1]
        for token in PLACEHOLDER_PATTERN.findall(masked)
        if token not in already
    }
    kinds.discard(EMAIL)
    return tuple(sorted(kinds))
