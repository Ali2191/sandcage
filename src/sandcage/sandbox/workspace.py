"""The host-side copy of the target repo for one run.

The repo is copied into a fresh temp directory ``<temp>/sandcage-XXXX/workspace`` through
:mod:`safefs` (links copied as links, never followed). The original is never written.
When the gate is enforcing, secret files (the rulebook's ``secrets``) are left out of the
copy, so "secrets are not in the sandbox" is literally true; observe-only runs keep them
(the Bench baseline needs its fake token in place for secret-theft attacks to land).

Every host-side touch of the copy goes through :mod:`safefs`; error messages name only
workspace-relative paths. The temp directory is deleted on cleanup, and the reaper may
delete one only when :func:`run_temp_dir` accepts its exact shape.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Final

from sandcage.policy.matchers import MatchContext
from sandcage.policy.paths import Workspace as AgentPaths
from sandcage.sandbox import safefs

TEMP_PREFIX: Final = "sandcage-"
WORKSPACE_DIR: Final = "workspace"
AGENT_ROOT: Final = AgentPaths(PurePosixPath("/workspace"))
READ_LIMIT: Final = 100_000
VIEW_LIMIT: Final = 1_000_000
WRITE_LIMIT: Final = 1_000_000
MAX_FILES: Final = 10_000
MAX_BYTES: Final = 50 * 1024 * 1024
RUN_DIR_MODE: Final = 0o711  # the sandbox user may pass through, nobody else may list
WORKSPACE_MODE: Final = 0o777

_RUN_DIR_NAME: Final = re.compile(re.escape(TEMP_PREFIX) + r"[A-Za-z0-9_]+")


class WorkspaceError(Exception):
    """A workspace operation failed. The message is safe to show: no host paths."""


@dataclass(frozen=True)
class FileRead:
    content: str
    truncated: bool


def secret_checker(context: MatchContext) -> Callable[[str], bool]:
    """A predicate on workspace-relative paths: is this a secret file per the rulebook?"""
    agent, secrets = context.workspace, context.secrets
    return lambda relative: secrets.matches(agent, agent.root / relative)


def _never(_: str) -> bool:
    return False


class Workspace:
    """One run's copy of the repo. Use :meth:`create`; call :meth:`cleanup` (or use it as a
    context manager) to delete it."""

    def __init__(self, run_dir: Path, left_out: tuple[str, ...], agent: AgentPaths) -> None:
        self._run_dir = run_dir
        self._path = run_dir / WORKSPACE_DIR
        self._left_out = left_out
        self._agent = agent

    @classmethod
    def create(
        cls,
        repo: Path,
        *,
        leave_out: Callable[[str], bool] | None = None,
        temp_root: Path | None = None,
        agent: AgentPaths = AGENT_ROOT,
        max_files: int = MAX_FILES,
        max_bytes: int = MAX_BYTES,
    ) -> Workspace:
        """Copy ``repo`` into a new temp workspace. On any failure nothing is left behind."""
        try:
            info = os.lstat(repo)
        except OSError:
            raise WorkspaceError("the repository does not exist") from None
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError("the repository is not a directory")
        root = Path(os.path.realpath(temp_root or tempfile.gettempdir()))
        try:
            run_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=root))
        except OSError as problem:
            raise WorkspaceError(f"cannot create a temp workspace: {problem.strerror}") from None
        try:
            run_dir.chmod(RUN_DIR_MODE)
            path = run_dir / WORKSPACE_DIR
            path.mkdir()
            path.chmod(WORKSPACE_MODE)
            report = safefs.copy_tree(
                repo,
                path,
                leave_out=leave_out or _never,
                max_files=max_files,
                max_bytes=max_bytes,
            )
        except BaseException as problem:
            note = "" if _remove_tree(run_dir) else " (and the partial copy could not be deleted)"
            if isinstance(problem, safefs.FileRefusedError):
                raise WorkspaceError(f"cannot copy the repository: {problem}{note}") from None
            if isinstance(problem, OSError):
                reason = problem.strerror or "file system error"
                raise WorkspaceError(f"cannot copy the repository: {reason}{note}") from None
            raise
        return cls(run_dir, tuple(sorted(report.left_out)), agent)

    # --- identity ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The host path of the copy (bind-mounted at /workspace in the container)."""
        return self._path

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def left_out(self) -> tuple[str, ...]:
        """Relative paths of secret files (or directories) not copied."""
        return self._left_out

    @property
    def exists(self) -> bool:
        return self._run_dir.is_dir()

    def relative(self, agent_path: str) -> str:
        """Map an agent path (relative, or absolute under /workspace) to a relative one."""
        resolved = self._agent.resolve(agent_path)
        if resolved is None or not self._agent.contains(resolved):
            raise WorkspaceError(f"{agent_path} is outside the workspace")
        return self._agent.relative(resolved)

    # --- file tools ----------------------------------------------------------------

    def read_file(self, agent_path: str, *, limit: int = READ_LIMIT) -> FileRead:
        relative = self.relative(agent_path)
        try:
            data, truncated = safefs.read_bytes(self._path, relative, limit=limit)
        except (safefs.UnsafePathError, safefs.FileRefusedError) as problem:
            raise WorkspaceError(str(problem)) from None
        return FileRead(data.decode("utf-8", errors="replace"), truncated)

    def write_file(self, agent_path: str, content: str) -> int:
        relative = self.relative(agent_path)
        data = content.encode("utf-8")
        if len(data) > WRITE_LIMIT:
            raise WorkspaceError(f"{relative}: the content is larger than {WRITE_LIMIT} bytes")
        try:
            return safefs.write_bytes(self._path, relative, data)
        except (safefs.UnsafePathError, safefs.FileRefusedError) as problem:
            raise WorkspaceError(str(problem)) from None

    def list_dir(self, agent_path: str = ".") -> list[safefs.Entry]:
        relative = self.relative(agent_path)
        try:
            return safefs.list_dir(self._path, relative)
        except (safefs.UnsafePathError, safefs.FileRefusedError) as problem:
            raise WorkspaceError(str(problem)) from None

    # --- the policy's view ---------------------------------------------------------

    def read_text(self, path: str) -> str | None:
        """Contents of the workspace-relative ``path``; None when there is no such regular
        file (a link, FIFO or directory counts as no file, which the gate treats as a new
        file: the stricter reading)."""
        data = safefs.try_read(self._path, path, limit=VIEW_LIMIT)
        return None if data is None else data.decode("utf-8", errors="replace")

    # --- lifetime ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Delete the whole temp directory. Safe to call twice."""
        if self._run_dir.exists():
            try:
                shutil.rmtree(self._run_dir)
            except OSError as problem:
                raise WorkspaceError(
                    f"cannot delete the temp workspace: {problem.strerror or 'file system error'}"
                ) from None

    def __enter__(self) -> Workspace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()


def _remove_tree(path: Path) -> bool:
    """Remove a failed create's temp directory; False if it could not be removed (the
    caller reports that next to the original error)."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def run_temp_dir(workspace_path: str, temp_root: Path | None = None) -> Path | None:
    """The run directory to delete for ``workspace_path``, or None if it is not exactly a
    workspace this user created: ``<temp root>/sandcage-XXXX/workspace``, normalised,
    real directories (no links), owned by us."""
    root = Path(os.path.realpath(temp_root or tempfile.gettempdir()))
    if not workspace_path.startswith("/") or os.path.normpath(workspace_path) != workspace_path:
        return None
    path = Path(workspace_path)
    run_dir = path.parent
    if path.name != WORKSPACE_DIR or run_dir.parent != root:
        return None
    if not _RUN_DIR_NAME.fullmatch(run_dir.name):
        return None
    for candidate in (run_dir, path):
        try:
            info = os.lstat(candidate)
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return None
    return run_dir
