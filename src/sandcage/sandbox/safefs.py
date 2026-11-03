"""Symlink-race-proof file access for every host-side touch of a workspace.

The sandbox can create, delete or swap files at any moment, even turning a directory into
a symlink to a host path between a check and a use. So nothing here resolves a path and
then uses it. Every operation opens the root, then each directory component with
``O_NOFOLLOW | O_DIRECTORY`` relative to the previous directory's descriptor, and finally
opens the file itself with ``O_NOFOLLOW`` relative to its parent. A symlink anywhere on
the way is refused, even one that points inside the workspace (following it would mean
resolving it). Files are opened non-blocking and checked to be regular, so a FIFO the
sandbox planted cannot hang the host.

Paths are workspace-relative strings (``"src/app.py"``). Error messages name only that
relative path, never a host path.

Permissions: the container runs as an unprivileged user that does not own the host's
files, so new directories get mode 0o777 and files keep their mode OR 0o666. The run's
temp directory itself is not listable by other users (see :mod:`workspace`).
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

SKIP_DIRS: Final = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".tox",
        ".venv",
        "node_modules",
    }
)
DIR_MODE: Final = 0o777
FILE_MODE: Final = 0o666
MAX_DEPTH: Final = 64
LINK_ERRNOS: Final = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK: Final = 64 * 1024


class UnsafePathError(ValueError):
    """The path text is not a plain relative path (empty, absolute, ``..``, NUL)."""


class FileRefusedError(OSError):
    """An operation was refused safely. ``path`` is workspace-relative."""

    def __init__(self, path: str, problem: str, code: int | None = None) -> None:
        super().__init__(code if code is not None else 0, f"{path}: {problem}")
        self.path = path
        self.problem = problem

    def __str__(self) -> str:
        return f"{self.path}: {self.problem}"


class MissingPathError(FileRefusedError):
    def __init__(self, path: str) -> None:
        super().__init__(path, "no such file or directory", errno.ENOENT)


class LinkInPathError(FileRefusedError):
    def __init__(self, path: str) -> None:
        super().__init__(path, "a symbolic link is in the way, and links are never followed")


class NotRegularFileError(FileRefusedError):
    def __init__(self, path: str, what: str = "not a regular file") -> None:
        super().__init__(path, what)


class LimitExceededError(FileRefusedError):
    """A copy grew past its file-count or byte limit."""


class EntryKind(StrEnum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True)
class Entry:
    name: str
    kind: EntryKind


@dataclass
class CopyReport:
    """What :func:`copy_tree` copied. Paths are relative to the source root."""

    files: int = 0
    bytes: int = 0
    links: int = 0
    left_out: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# --- paths ---------------------------------------------------------------------------


def parts_of(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    """Split a relative path into components; ``.`` and empty parts are dropped.

    Refuses NUL, absolute paths and ``..``. An empty result (``""``, ``"."``) is refused
    unless ``allow_root`` (for listing the workspace root).
    """
    if "\x00" in path:
        raise UnsafePathError("the path contains a NUL character")
    if path.startswith("/"):
        raise UnsafePathError("the path must be relative to the workspace")
    parts = tuple(part for part in path.split("/") if part not in {"", "."})
    if ".." in parts:
        raise UnsafePathError("the path may not contain '..'")
    if not parts and not allow_root:
        raise UnsafePathError("the path does not name a file")
    return parts


def _shown(parts: tuple[str, ...]) -> str:
    return "/".join(parts) or "."


# --- directory walking ---------------------------------------------------------------


def _open_root(root: Path) -> int:
    try:
        return os.open(root, _DIR_FLAGS)
    except OSError as problem:
        raise FileRefusedError(".", "the workspace root cannot be opened", problem.errno) from None


def _explain(parent: int, name: str, shown: str, problem: OSError) -> FileRefusedError:
    """Turn an OSError from a dir-relative call into a refusal that names no host path."""
    if problem.errno == errno.ENOENT:
        return MissingPathError(shown)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        info = None
    if info is not None and stat.S_ISLNK(info.st_mode):
        return LinkInPathError(shown)
    if problem.errno == errno.EISDIR or (info is not None and stat.S_ISDIR(info.st_mode)):
        return NotRegularFileError(shown, "is a directory")
    if problem.errno == errno.ENOTDIR and info is not None and stat.S_ISREG(info.st_mode):
        return NotRegularFileError(shown, "is a file, not a directory")
    if problem.errno in {errno.ENXIO, errno.EOPNOTSUPP} or (
        info is not None and not stat.S_ISREG(info.st_mode)
    ):
        return NotRegularFileError(shown)
    if problem.errno in LINK_ERRNOS:
        return LinkInPathError(shown)
    return FileRefusedError(shown, os.strerror(problem.errno or errno.EIO), problem.errno)


def _enter(parent: int, name: str, shown: str, *, create: bool) -> int:
    """Open the directory ``name`` under ``parent`` without following links."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise MissingPathError(shown) from None
    except OSError as problem:
        raise _explain(parent, name, shown, problem) from None
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent)
    except FileExistsError:
        pass  # created concurrently; the open below decides what it is
    except OSError as problem:
        raise _explain(parent, name, shown, problem) from None
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as problem:
        raise _explain(parent, name, shown, problem) from None
    os.fchmod(fd, DIR_MODE)
    return fd


@contextmanager
def directory(root: Path, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
    """A descriptor for ``root/parts...``, walked component by component, never through
    a link. With ``create``, missing directories are made."""
    fd = _open_root(root)
    try:
        for index, name in enumerate(parts):
            child = _enter(fd, name, _shown(parts[: index + 1]), create=create)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


# --- files ---------------------------------------------------------------------------


def _open_file(parent: int, name: str, shown: str, flags: int) -> int:
    try:
        return os.open(name, flags, FILE_MODE, dir_fd=parent)
    except OSError as problem:
        raise _explain(parent, name, shown, problem) from None


def _require_regular(fd: int, shown: str) -> os.stat_result:
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        raise NotRegularFileError(shown, "is a directory")
    if not stat.S_ISREG(info.st_mode):
        raise NotRegularFileError(shown)
    return info


def _read_up_to(fd: int, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = os.read(fd, min(_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_bytes(root: Path, path: str, *, limit: int) -> tuple[bytes, bool]:
    """The first ``limit`` bytes of a regular file and whether it was longer."""
    parts = parts_of(path)
    shown = _shown(parts)
    with directory(root, parts[:-1]) as parent:
        fd = _open_file(parent, parts[-1], shown, _READ_FLAGS)
        try:
            _require_regular(fd, shown)
            data = _read_up_to(fd, limit + 1)
        finally:
            os.close(fd)
    return data[:limit], len(data) > limit


def sha256(root: Path, path: str) -> str:
    """The SHA-256 of a whole regular file, read in chunks through the same no-follow walk."""
    parts = parts_of(path)
    shown = _shown(parts)
    digest = hashlib.sha256()
    with directory(root, parts[:-1]) as parent:
        fd = _open_file(parent, parts[-1], shown, _READ_FLAGS)
        try:
            _require_regular(fd, shown)
            while chunk := os.read(fd, _CHUNK):
                digest.update(chunk)
        finally:
            os.close(fd)
    return digest.hexdigest()


def try_read(root: Path, path: str, *, limit: int) -> bytes | None:
    """Like :func:`read_bytes`, but None when the file is missing, unsafe or not regular."""
    try:
        data, _ = read_bytes(root, path, limit=limit)
    except (UnsafePathError, FileRefusedError):
        return None
    return data


def write_bytes(root: Path, path: str, data: bytes, *, replace_links: bool = False) -> int:
    """Write a regular file, creating missing directories. A link at the target is refused
    (or, with ``replace_links``, removed and replaced by a file; its target is untouched)."""
    parts = parts_of(path)
    shown = _shown(parts)
    with directory(root, parts[:-1], create=True) as parent:
        name = parts[-1]
        if replace_links:
            _unlink_if_link(parent, name, shown)
        fd = _open_file(parent, name, shown, _WRITE_FLAGS)
        try:
            info = _require_regular(fd, shown)
            os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, stat.S_IMODE(info.st_mode) | FILE_MODE)
        finally:
            os.close(fd)
    return len(data)


def _unlink_if_link(parent: int, name: str, shown: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as problem:
        raise _explain(parent, name, shown, problem) from None
    if stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent)


def remove(root: Path, path: str) -> None:
    """Remove a file or a link (never what a link points to). Directories are refused."""
    parts = parts_of(path)
    shown = _shown(parts)
    with directory(root, parts[:-1]) as parent:
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                raise NotRegularFileError(shown, "is a directory")
            os.unlink(name, dir_fd=parent)
        except FileRefusedError:
            raise
        except OSError as problem:
            raise _explain(parent, name, shown, problem) from None


def mode_of(root: Path, path: str) -> int:
    """The ``st_mode`` of a path, without following a final link."""
    parts = parts_of(path)
    shown = _shown(parts)
    with directory(root, parts[:-1]) as parent:
        try:
            return os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
        except OSError as problem:
            raise _explain(parent, parts[-1], shown, problem) from None


def _kind(entry: os.DirEntry[str]) -> EntryKind:
    if entry.is_symlink():
        return EntryKind.SYMLINK
    if entry.is_dir(follow_symlinks=False):
        return EntryKind.DIR
    if entry.is_file(follow_symlinks=False):
        return EntryKind.FILE
    return EntryKind.OTHER


def _entries(fd: int) -> list[os.DirEntry[str]]:
    with os.scandir(fd) as found:
        return sorted(found, key=lambda entry: entry.name)


def list_dir(root: Path, path: str) -> list[Entry]:
    """Entries of a directory (``"."`` is the root), sorted by name."""
    parts = parts_of(path, allow_root=True)
    with directory(root, parts) as fd:
        return [Entry(entry.name, _kind(entry)) for entry in _entries(fd)]


# --- walking -------------------------------------------------------------------------


def walk(
    root: Path, *, include_links: bool = False, skip_dirs: frozenset[str] = SKIP_DIRS
) -> Iterator[str]:
    """Relative paths of regular files under ``root``, sorted per directory.

    Never enters a symlinked directory: each directory is opened from its parent's
    descriptor with no-follow, so a directory swapped for a link mid-walk is skipped.
    Links are yielded as names only when ``include_links``. Other file types are skipped.
    """
    fd = _open_root(root)
    try:
        yield from _walk_fd(fd, (), include_links, skip_dirs)
    finally:
        os.close(fd)


def _walk_fd(
    fd: int, prefix: tuple[str, ...], include_links: bool, skip_dirs: frozenset[str]
) -> Iterator[str]:
    for entry in _entries(fd):
        kind = _kind(entry)
        relative = (*prefix, entry.name)
        if kind is EntryKind.FILE or (kind is EntryKind.SYMLINK and include_links):
            yield "/".join(relative)
        elif kind is EntryKind.DIR and entry.name not in skip_dirs and len(relative) < MAX_DEPTH:
            try:
                child = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
            except OSError:
                continue  # gone or swapped for a link since it was listed: never followed
            try:
                yield from _walk_fd(child, relative, include_links, skip_dirs)
            finally:
                os.close(child)


# --- copying -------------------------------------------------------------------------


def copy_tree(
    source: Path,
    target: Path,
    *,
    leave_out: Callable[[str], bool],
    max_files: int,
    max_bytes: int,
    skip_dirs: frozenset[str] = SKIP_DIRS,
) -> CopyReport:
    """Copy ``source`` into the existing empty directory ``target``.

    Links are copied as links (never followed); FIFOs, sockets and devices are skipped;
    ``skip_dirs`` are not copied. A file, link or directory for which ``leave_out`` is
    true (relative path) is not copied and is listed in ``left_out``. Raises
    :class:`LimitExceededError` past ``max_files`` or ``max_bytes``.
    """
    copier = _Copier(leave_out, max_files, max_bytes, skip_dirs)
    src_fd = _open_root(source)
    try:
        dst_fd = _open_root(target)
        try:
            copier.copy_dir(src_fd, dst_fd, ())
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)
    return copier.report


@dataclass
class _Copier:
    leave_out: Callable[[str], bool]
    max_files: int
    max_bytes: int
    skip_dirs: frozenset[str]
    report: CopyReport = field(default_factory=CopyReport)

    def copy_dir(self, src: int, dst: int, prefix: tuple[str, ...]) -> None:
        report = self.report
        for entry in _entries(src):
            relative = (*prefix, entry.name)
            shown = _shown(relative)
            kind = _kind(entry)
            if kind is EntryKind.DIR and entry.name in self.skip_dirs:
                continue
            if kind is EntryKind.OTHER or (kind is EntryKind.DIR and len(relative) >= MAX_DEPTH):
                report.skipped.append(shown)
                continue
            if self.leave_out(shown):
                report.left_out.append(shown)
                continue
            if report.files + report.links >= self.max_files:
                raise LimitExceededError(
                    shown, f"the repository has more than {self.max_files} files"
                )
            if kind is EntryKind.SYMLINK:
                os.symlink(os.readlink(entry.name, dir_fd=src), entry.name, dir_fd=dst)
                report.links += 1
            elif kind is EntryKind.FILE:
                budget = self.max_bytes - report.bytes
                report.bytes += _copy_file(src, dst, entry.name, shown, budget)
                report.files += 1
            else:
                self._copy_subdir(src, dst, entry.name, relative)

    def _copy_subdir(self, src: int, dst: int, name: str, relative: tuple[str, ...]) -> None:
        shown = _shown(relative)
        child_src = _enter(src, name, shown, create=False)
        try:
            child_dst = _enter(dst, name, shown, create=True)
            try:
                self.copy_dir(child_src, child_dst, relative)
            finally:
                os.close(child_dst)
        finally:
            os.close(child_src)


def _copy_file(src: int, dst: int, name: str, shown: str, budget: int) -> int:
    in_fd = _open_file(src, name, shown, _READ_FLAGS)
    try:
        info = _require_regular(in_fd, shown)
        out_fd = _open_file(dst, name, shown, _WRITE_FLAGS | os.O_EXCL)
        try:
            copied = 0
            while chunk := os.read(in_fd, _CHUNK):
                copied += len(chunk)
                if copied > budget:
                    raise LimitExceededError(shown, "the repository is larger than the copy limit")
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out_fd, view) :]
            os.fchmod(out_fd, stat.S_IMODE(info.st_mode) | FILE_MODE)
        finally:
            os.close(out_fd)
    finally:
        os.close(in_fd)
    return copied
