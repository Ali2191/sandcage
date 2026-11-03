"""The one glob matcher for workspace-relative paths, used by every policy layer.

Semantics (close to .gitignore):

- A pattern without a slash matches any single path component, at any depth:
  ``*.pem`` matches ``keys/server.pem``; ``tests`` matches ``tests/test_a.py``.
- A pattern with a slash is anchored at the workspace root; ``*`` and ``?`` stay inside one
  component and ``**`` spans any number of components (including none):
  ``src/*.py`` matches ``src/a.py`` but not ``src/x/a.py``; ``**/.git/config`` matches
  ``.git/config`` and ``vendor/lib/.git/config``.
- Either way, a match on a directory covers everything below it: ``invoice_calc/**`` and
  ``invoice_calc/`` both cover ``invoice_calc/calc.py``.
"""

from __future__ import annotations

from fnmatch import fnmatchcase


def glob_match(pattern: str, path: str, *, case_sensitive: bool = True) -> bool:
    """True when the relative ``path`` is matched (or sits below a match) by ``pattern``."""
    pattern = pattern.strip().strip("/")
    path = path.strip("/")
    if not pattern or not path:
        return False
    if not case_sensitive:
        pattern = pattern.lower()
        path = path.lower()
    parts = _parts(path)
    if "/" not in pattern:
        return any(fnmatchcase(part, pattern) for part in parts)
    pattern_parts = _parts(pattern)
    return bool(pattern_parts) and _match_anchored(pattern_parts, parts)


def matches_any(patterns: tuple[str, ...], path: str, *, case_sensitive: bool = True) -> bool:
    return any(glob_match(p, path, case_sensitive=case_sensitive) for p in patterns)


def _parts(text: str) -> list[str]:
    return [part for part in text.split("/") if part not in {"", "."}]


def _match_anchored(pattern_parts: list[str], path_parts: list[str]) -> bool:
    """Does the pattern match the path or one of its leading directories?"""
    if not pattern_parts:
        return True  # the pattern matched a directory the path sits in
    head, rest = pattern_parts[0], pattern_parts[1:]
    if head == "**":
        return any(_match_anchored(rest, path_parts[skip:]) for skip in range(len(path_parts) + 1))
    if not path_parts:
        return False
    return fnmatchcase(path_parts[0], head) and _match_anchored(rest, path_parts[1:])
