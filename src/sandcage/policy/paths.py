"""Static path reasoning: resolve agent paths and recognise secret files.

Nothing here touches the file system. A path is resolved lexically against the workspace
root; anything that cannot be known without a shell (``~``, ``$VAR``, substitutions, NUL)
is unknown, and callers treat unknown as outside the workspace.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from sandcage.policy.globs import matches_any

_UNKNOWABLE = ("$", "`", "\x00")


@dataclass(frozen=True)
class Workspace:
    """The agent-facing workspace root (``/workspace``)."""

    root: PurePosixPath

    def resolve(self, path: str) -> PurePosixPath | None:
        """Absolute, normalised path; None when it cannot be known statically."""
        if not path or path.startswith("~") or any(mark in path for mark in _UNKNOWABLE):
            return None
        joined = path if path.startswith("/") else posixpath.join(str(self.root), path)
        normal = posixpath.normpath(joined)
        if normal.startswith("//"):
            normal = "/" + normal.lstrip("/")
        return PurePosixPath(normal)

    def contains(self, path: PurePosixPath) -> bool:
        return path == self.root or self.root in path.parents

    def relative(self, path: PurePosixPath) -> str:
        """Workspace-relative form of an inside path (``"."`` for the root itself)."""
        return str(path.relative_to(self.root))

    def label(self, path: str) -> str:
        """How a path is named in reasons and taint sources: relative when inside."""
        resolved = self.resolve(path)
        if resolved is not None and self.contains(resolved):
            return self.relative(resolved)
        return path


@dataclass(frozen=True)
class SecretFiles:
    """Secret file globs (case-insensitive, see :mod:`globs`) plus a name pattern applied
    to every path component."""

    globs: tuple[str, ...]
    name_pattern: re.Pattern[str]

    def matches(self, workspace: Workspace, path: PurePosixPath) -> bool:
        """True for a secret path, judged relative to the workspace when inside it."""
        text = workspace.relative(path) if workspace.contains(path) else str(path)
        if matches_any(self.globs, text, case_sensitive=False):
            return True
        return any(self.name_pattern.search(part.lower()) for part in path.parts if part != "/")

    def glob_may_match(self, pattern: str) -> bool:
        """Could a shell glob argument (``.env*``, ``*.pem``) expand to a secret file?

        A bare ``*``-style pattern is not treated as one (it would make ``ls *`` a denial);
        the sandbox copy holds no secret files anyway when the gate is enforcing.
        """
        last = pattern.rstrip("/").rsplit("/", 1)[-1].lower()
        if not last or set(last) <= {"*", "."}:
            return False
        return any(fnmatchcase(sample, last) for sample in _SECRET_SAMPLES)


_SECRET_SAMPLES = (
    ".env",
    ".env.local",
    "prod.env",
    ".envrc",
    "id_rsa",
    "id_ed25519",
    "server.pem",
    "server.key",
    "cert.p12",
    "cert.pfx",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".htpasswd",
    ".git-credentials",
    "secrets.yaml",
    "credentials.json",
)
