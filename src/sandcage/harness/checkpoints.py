"""Checkpoints: file copies of the workspace after every successful write, and restores.

Checkpoint 0 is the original repo. Each later checkpoint records every regular file that
differs from the original at that moment (its bytes and mode, or "deleted"), so restoring
N reproduces the files exactly as they were at N, whatever happened since. Later
checkpoints are kept after a restore, so a rewind can itself be undone.

**File copies, not git** (History/Bugs #10): running git on the host in a directory the
agent can write to would run whatever the agent put in ``.git/hooks``. Nothing here runs
a tool; every touch goes through :mod:`safefs` (History/Bugs #11), so no link is ever
followed. A restore replaces a link that sits where a file must go (the link's target is
untouched) and refuses, loudly, when a directory on the way is a link.

Contents are stored once per distinct file content (by SHA-256), and the stored total is
capped at :data:`CHECKPOINT_CAP` bytes per run. A save that would cross the cap raises
:class:`CheckpointError` and stores nothing: it never silently skips. Links are not part
of a checkpoint: they are never copied or followed, and a restore leaves a link alone
unless a file has to be written at its path. Empty directories are not tracked.
"""

from __future__ import annotations

import hashlib
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from sandcage.contract.common import Phase
from sandcage.contract.events import CheckpointEvent, CheckpointState
from sandcage.masking import mask
from sandcage.policy.actions import Action
from sandcage.sandbox import safefs

CHECKPOINT_CAP: Final = 20 * 1024 * 1024
FILE_LIMIT: Final = CHECKPOINT_CAP


class CheckpointError(Exception):
    """A checkpoint could not be saved or restored. The message is safe to show."""


@dataclass(frozen=True)
class FileCopy:
    digest: str
    mode: int


Snapshot = dict[str, FileCopy | None]  # path -> copy, or None for "deleted"


@dataclass(frozen=True)
class CheckpointInfo:
    id: int
    step: int | None
    files: tuple[str, ...]  # files that differ from the original at this checkpoint
    created: float


@dataclass
class Checkpoints:
    original: Path
    _workspace: Path | None = None
    left_out: frozenset[str] = frozenset()
    cap: int = CHECKPOINT_CAP
    run_id: str = ""
    clock: Callable[[], float] = time.time
    _blobs: dict[str, bytes] = field(default_factory=dict)
    _snapshots: list[Snapshot] = field(default_factory=list)
    _infos: list[CheckpointInfo] = field(default_factory=list)
    _events: list[CheckpointEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._snapshots.append({})
        self._infos.append(CheckpointInfo(0, None, (), self.clock()))

    def attach(self, workspace: Path, left_out: Iterable[str] = ()) -> None:
        """Bind the run's workspace copy (it exists only once the run is open)."""
        self._workspace = workspace
        self.left_out = frozenset(left_out)

    @property
    def workspace(self) -> Path:
        if self._workspace is None:
            raise CheckpointError("checkpoints are not attached to a workspace yet")
        return self._workspace

    # --- reading -------------------------------------------------------------------------

    @property
    def stored_bytes(self) -> int:
        return sum(len(blob) for blob in self._blobs.values())

    @property
    def latest(self) -> int:
        return len(self._snapshots) - 1

    def infos(self) -> list[CheckpointInfo]:
        return list(self._infos)

    def get(self, checkpoint: object) -> CheckpointInfo:
        if (
            not isinstance(checkpoint, int)
            or isinstance(checkpoint, bool)
            or not 0 <= checkpoint < len(self._infos)
        ):
            known = ", ".join(str(info.id) for info in self._infos)
            raise CheckpointError(f"there is no checkpoint {checkpoint!r}; known: {known}")
        return self._infos[checkpoint]

    # --- saving --------------------------------------------------------------------------

    def save(self, step: int | None = None) -> CheckpointInfo:
        """Snapshot the workspace. An unchanged workspace reuses the latest checkpoint."""
        snapshot, new_blobs = self._current()
        if snapshot == self._snapshots[-1]:
            return self._infos[-1]
        added = sum(len(blob) for blob in new_blobs.values())
        if self.stored_bytes + added > self.cap:
            raise CheckpointError(
                f"checkpoint not saved: it needs {added} more bytes and checkpoints are "
                f"capped at {self.cap} bytes per run"
            )
        self._blobs.update(new_blobs)
        self._snapshots.append(snapshot)
        info = CheckpointInfo(len(self._infos), step, tuple(sorted(snapshot)), self.clock())
        self._infos.append(info)
        return info

    def _current(self) -> tuple[Snapshot, dict[str, bytes]]:
        snapshot: Snapshot = {}
        new_blobs: dict[str, bytes] = {}
        budget = self.cap - self.stored_bytes
        present = set(self._files(self.workspace))
        for path in sorted(present):
            digest = self._digest(self.workspace, path)
            mode = stat.S_IMODE(self._mode(self.workspace, path)) | safefs.FILE_MODE
            if self._same_as_original(path, digest, mode):
                continue
            snapshot[path] = FileCopy(digest, mode)
            if digest in self._blobs or digest in new_blobs:
                continue
            data = self._read(self.workspace, path, limit=max(budget, 0))
            if data is None:
                raise CheckpointError(
                    f"checkpoint not saved: {mask(path)} would cross the {self.cap}-byte cap"
                )
            if hashlib.sha256(data).hexdigest() != digest:
                raise CheckpointError(f"checkpoint not saved: {mask(path)} changed while saving")
            new_blobs[digest] = data
            budget -= len(data)
        for path in self._files(self.original):
            if path not in present and path not in self.left_out:
                snapshot[path] = None
        return snapshot, new_blobs

    def _same_as_original(self, path: str, digest: str, mode: int) -> bool:
        try:
            original_digest = safefs.sha256(self.original, path)
            original_mode = stat.S_IMODE(safefs.mode_of(self.original, path))
        except (safefs.UnsafePathError, safefs.FileRefusedError):
            return False
        return original_digest == digest and original_mode | safefs.FILE_MODE == mode

    # --- restoring -----------------------------------------------------------------------

    def restore(self, checkpoint: object) -> list[str]:
        """Put every file back exactly as at ``checkpoint``; returns the paths changed."""
        target = self._snapshots[self.get(checkpoint).id]
        present = set(self._files(self.workspace))
        original = set(self._files(self.original))
        changed: list[str] = []
        for path in sorted(present | original | set(target)):
            if path in self.left_out:
                continue
            try:
                if self._restore_one(path, target, present=path in present, original=original):
                    changed.append(path)
            except (safefs.UnsafePathError, safefs.FileRefusedError) as problem:
                done = f" ({len(changed)} files were restored before it)" if changed else ""
                raise CheckpointError(
                    f"restore refused at {mask(path)}: {mask(str(problem))}{done}"
                ) from None
        return changed

    def _restore_one(
        self, path: str, target: Snapshot, *, present: bool, original: set[str]
    ) -> bool:
        """Bring one path to its state at the target; True when something changed."""
        if path in target:
            copy = target[path]
            if copy is None:
                return self._remove(path, present=present)
            if present and self._matches(path, copy.digest, copy.mode):
                return False
            data = self._blobs[copy.digest]
            mode = copy.mode
        elif path in original:
            digest = safefs.sha256(self.original, path)
            mode = stat.S_IMODE(safefs.mode_of(self.original, path)) | safefs.FILE_MODE
            if present and self._matches(path, digest, mode):
                return False
            read = self._read(self.original, path, limit=FILE_LIMIT)
            if read is None:
                raise CheckpointError(f"restore refused: {mask(path)} is too large to restore")
            data = read
        else:
            return self._remove(path, present=present)
        safefs.write_bytes(self.workspace, path, data, replace_links=True, mode=mode)
        return True

    def _remove(self, path: str, *, present: bool) -> bool:
        if not present:
            return False
        safefs.remove(self.workspace, path)
        return True

    def _matches(self, path: str, digest: str, mode: int) -> bool:
        try:
            same = safefs.sha256(self.workspace, path) == digest
            return same and stat.S_IMODE(safefs.mode_of(self.workspace, path)) == mode
        except (safefs.UnsafePathError, safefs.FileRefusedError):
            return False

    # --- hooks for the run ---------------------------------------------------------------

    def on_write(self, _path: str) -> int | None:
        """``SandboxRun(on_write=...)``: save after a successful write. A failed save is
        a ``failed`` checkpoint event (announced after the step), never a silent skip."""
        try:
            info = self.save()
        except CheckpointError as problem:
            self._events.append(self._event(self.latest, CheckpointState.FAILED, str(problem)))
            return None
        self._events.append(self._event(info.id, CheckpointState.SAVED, files=info.files))
        return info.id

    def rollback(self, action: Action) -> JsonValue:
        """The agent's ``rollback`` tool (``SandboxRun(tools={"rollback": ...})``)."""
        return self.rewind(action.args.get("checkpoint"), by="agent")

    def rewind(self, checkpoint: object, *, by: str) -> JsonValue:
        try:
            restored = self.restore(checkpoint)
        except CheckpointError as problem:
            if (
                isinstance(checkpoint, int)
                and not isinstance(checkpoint, bool)
                and 0 <= checkpoint < len(self._infos)
            ):  # a known checkpoint that failed to restore (an unknown id is only an error)
                failed = self._event(checkpoint, CheckpointState.FAILED, str(problem))
                self._events.append(failed)
            return {"error": str(problem)}
        assert isinstance(checkpoint, int)  # noqa: S101 - restore validated it
        restored_by = "human" if by == "human" else "agent"
        event = self._event(checkpoint, CheckpointState.RESTORED, files=tuple(restored))
        self._events.append(event.model_copy(update={"restored_by": restored_by}))
        files: list[JsonValue] = [mask(path) for path in restored]
        return {"checkpoint": checkpoint, "restored": files}

    def drain_events(self) -> list[CheckpointEvent]:
        """Checkpoint events since the last call, for the harness to emit after the step."""
        events, self._events = self._events, []
        return events

    def summary(self) -> str:
        """The checkpoint list the agent is offered after a failed verification."""
        lines = ["Checkpoints you can roll back to:", "- 0: the original repo"]
        for info in self._infos[1:]:
            files = ", ".join(info.files[:5]) + (" ..." if len(info.files) > 5 else "")
            step = f"after step {info.step}" if info.step is not None else "saved"
            lines.append(f"- {info.id}: {step} ({mask(files) or 'no changes'})")
        return "\n".join(lines)

    # --- helpers -------------------------------------------------------------------------

    def _event(
        self,
        checkpoint: int,
        state: CheckpointState,
        error: str | None = None,
        files: Iterable[str] = (),
    ) -> CheckpointEvent:
        return CheckpointEvent(
            run_id=self.run_id,
            phase=Phase.ACT,
            ts=self.clock(),
            checkpoint=checkpoint,
            state=state,
            files=[mask(path) for path in files],
            error=mask(error) if error is not None else None,
        )

    @staticmethod
    def _files(root: Path) -> list[str]:
        return list(safefs.walk(root))

    @staticmethod
    def _digest(root: Path, path: str) -> str:
        try:
            return safefs.sha256(root, path)
        except (safefs.UnsafePathError, safefs.FileRefusedError) as problem:
            raise CheckpointError(f"checkpoint not saved: {mask(str(problem))}") from None

    @staticmethod
    def _mode(root: Path, path: str) -> int:
        try:
            return safefs.mode_of(root, path)
        except (safefs.UnsafePathError, safefs.FileRefusedError) as problem:
            raise CheckpointError(f"checkpoint failed: {mask(str(problem))}") from None

    @staticmethod
    def _read(root: Path, path: str, *, limit: int) -> bytes | None:
        """The whole file, or None when it is longer than ``limit``."""
        try:
            data, truncated = safefs.read_bytes(root, path, limit=limit)
        except (safefs.UnsafePathError, safefs.FileRefusedError) as problem:
            raise CheckpointError(f"checkpoint failed: {mask(str(problem))}") from None
        return None if truncated else data
