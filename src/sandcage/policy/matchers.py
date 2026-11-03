"""The fixed set of named matchers a rulebook rule can use.

A rule's ``match`` maps matcher names to parameters; every matcher must hold (AND). Each
matcher applies to some tools only; a rule that pairs a matcher with a tool it can never
apply to is rejected when the rulebook loads.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import ClassVar, Final, Protocol
from urllib.parse import urlsplit

from sandcage.policy.actions import ALL_TOOLS, COMMAND_TOOLS, FETCH_TOOLS, PATH_TOOLS, Action
from sandcage.policy.commands import ParsedCommand, Segment, normalize_program
from sandcage.policy.globs import matches_any
from sandcage.policy.paths import SecretFiles, Workspace

_GLOB_CHARS: Final = frozenset("*?[")
_NOT_PATH_REDIRECTS: Final = frozenset({"<<", "<<-", "<<<"})
_OCTAL_MODE: Final = re.compile(r"[0-7]{1,4}")
_SYMBOLIC_CLAUSE: Final = re.compile(r"([ugoa]*)([-+=])([rwxXst]*)")
_HOST: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")


@dataclass(frozen=True)
class MatchContext:
    """Rulebook settings the matchers need."""

    workspace: Workspace
    secrets: SecretFiles
    safe_paths: frozenset[str]
    test_files: tuple[str, ...]
    delete_programs: frozenset[str]
    package_managers: Mapping[str, tuple[tuple[str, ...], ...]]


@dataclass(frozen=True)
class Subject:
    """The action under judgment, with its command parsed once."""

    action: Action
    command: ParsedCommand | None


class Matcher(Protocol):
    tools: ClassVar[frozenset[str]]

    def matches(self, subject: Subject, ctx: MatchContext) -> bool: ...


# --- parameter validation ------------------------------------------------------------


def _expect_true(name: str, param: object) -> None:
    if param is not True:
        raise ValueError(f"'{name}' takes the value true")


def _expect_strings(name: str, param: object) -> tuple[str, ...]:
    if (
        not isinstance(param, list)
        or not param
        or not all(isinstance(item, str) and item.strip() for item in param)
    ):
        raise ValueError(f"'{name}' takes a non-empty list of text values")
    return tuple(item.strip() for item in param)


def _expect_count(name: str, param: object) -> int:
    if not isinstance(param, int) or isinstance(param, bool) or param < 1:
        raise ValueError(f"'{name}' takes a whole number of 1 or more")
    return param


# --- path tools ----------------------------------------------------------------------


@dataclass(frozen=True)
class Always:
    tools: ClassVar[frozenset[str]] = ALL_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        return True


@dataclass(frozen=True)
class PathOutsideWorkspace:
    tools: ClassVar[frozenset[str]] = PATH_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        resolved = ctx.workspace.resolve(subject.action.path)
        return resolved is None or not ctx.workspace.contains(resolved)


@dataclass(frozen=True)
class PathSecret:
    tools: ClassVar[frozenset[str]] = PATH_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        resolved = ctx.workspace.resolve(subject.action.path)
        return resolved is not None and ctx.secrets.matches(ctx.workspace, resolved)


@dataclass(frozen=True)
class PathGlob:
    globs: tuple[str, ...]
    tools: ClassVar[frozenset[str]] = PATH_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        relative = _inside_relative(subject.action.path, ctx)
        return relative is not None and matches_any(self.globs, relative)


@dataclass(frozen=True)
class PathIsTest:
    tools: ClassVar[frozenset[str]] = PATH_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        relative = _inside_relative(subject.action.path, ctx)
        return relative is not None and matches_any(ctx.test_files, relative)


def _inside_relative(path: str, ctx: MatchContext) -> str | None:
    resolved = ctx.workspace.resolve(path)
    if resolved is None or not ctx.workspace.contains(resolved):
        return None
    return ctx.workspace.relative(resolved)


# --- commands ------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseError:
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        return subject.command is not None and subject.command.error is not None


@dataclass(frozen=True)
class ProgramIn:
    programs: frozenset[str]
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        return bool(_command(subject).programs & self.programs)


@dataclass(frozen=True)
class PipeTo:
    programs: frozenset[str]
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        return any(
            segment.piped and segment.possible_programs & self.programs
            for segment in _command(subject).segments
        )


@dataclass(frozen=True)
class ProgramWithRedirect:
    programs: frozenset[str]
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        command = _command(subject)
        return command.has_redirect and bool(command.programs & self.programs)


@dataclass(frozen=True)
class ChmodWorldWritable:
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        return any(
            "chmod" in segment.possible_programs and any(map(_world_writable, segment.argv[1:]))
            for segment in _command(subject).segments
        )


def _world_writable(mode: str) -> bool:
    if _OCTAL_MODE.fullmatch(mode):
        return bool(int(mode[-1], 8) & 0o2)
    for clause in mode.split(","):
        found = _SYMBOLIC_CLAUSE.fullmatch(clause)
        if found is None:
            continue
        who, op, perms = found.groups()
        if op in "+=" and "w" in perms and (not who or "o" in who or "a" in who):
            return True
    return False


@dataclass(frozen=True)
class DeleteOutsideWorkspace:
    """A delete program aimed outside the workspace, at its root, or at an unknown path."""

    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        for segment in _command(subject).segments:
            if segment.program in ctx.delete_programs:
                for target in _delete_targets(segment):
                    resolved = ctx.workspace.resolve(target)
                    if (
                        resolved is None
                        or not ctx.workspace.contains(resolved)
                        or resolved == ctx.workspace.root
                    ):
                        return True
            elif segment.possible_programs & ctx.delete_programs:
                return True  # a wrapper runs a delete the parser cannot place: fail closed
        return False


@dataclass(frozen=True)
class BulkDelete:
    """More than ``max_targets`` targets, a recursive delete, or wildcard targets."""

    max_targets: int
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        for segment in _command(subject).segments:
            if segment.program not in ctx.delete_programs:
                continue
            targets = _delete_targets(segment)
            if (
                len(targets) > self.max_targets
                or _recursive(segment)
                or any(_GLOB_CHARS & set(target) for target in targets)
            ):
                return True
        return False


def _delete_targets(segment: Segment) -> list[str]:
    targets: list[str] = []
    options_done = False
    for arg in segment.args:
        if not options_done and arg == "--":
            options_done = True
        elif options_done or not arg.startswith("-") or arg == "-":
            targets.append(arg)
    return targets


def _recursive(segment: Segment) -> bool:
    for arg in segment.args:
        if arg == "--":
            return False
        if arg == "--recursive" or (
            arg.startswith("-") and not arg.startswith("--") and ({"r", "R"} & set(arg[1:]))
        ):
            return True
    return False


@dataclass(frozen=True)
class SecretReference:
    """Any argument, redirect target or assignment that names a secret file."""

    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        for word in _path_words(_command(subject), include_programs=True):
            for candidate in _path_candidates(word):
                if any(char in candidate for char in _GLOB_CHARS):
                    if ctx.secrets.glob_may_match(candidate):
                        return True
                    continue
                resolved = ctx.workspace.resolve(candidate)
                if resolved is not None and ctx.secrets.matches(ctx.workspace, resolved):
                    return True
        return False


@dataclass(frozen=True)
class ArgOutsideWorkspace:
    """An argument that looks like a path outside the workspace (absolute, home, variable,
    or climbing with ``..``), other than the safe paths."""

    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        for word in _path_words(_command(subject), include_programs=False):
            for candidate in _path_candidates(word):
                if _outside(candidate, ctx):
                    return True
        return False


def _outside(candidate: str, ctx: MatchContext) -> bool:
    if candidate in ctx.safe_paths:
        return False
    looks_like_path = (
        candidate.startswith(("/", "~"))
        or "$" in candidate
        or ".." in PurePosixPath(candidate).parts
    )
    if not looks_like_path:
        return False
    resolved = ctx.workspace.resolve(candidate)
    if resolved is None:
        return True
    return str(resolved) not in ctx.safe_paths and not ctx.workspace.contains(resolved)


def _path_words(command: ParsedCommand, *, include_programs: bool) -> Iterator[str]:
    for segment in command.segments:
        words = segment.argv if include_programs else segment.argv[1:]
        yield from words
        for assignment in segment.assignments:
            yield assignment.split("=", 1)[1]
        for redirect in segment.redirects:
            if not redirect.is_fd_dup and redirect.op not in _NOT_PATH_REDIRECTS:
                yield redirect.target


def _path_candidates(word: str) -> list[str]:
    """The ways one word can name a file: itself, an ``--opt=value`` value, ``@file``."""
    if word.startswith("-"):
        return [word.split("=", 1)[1]] if "=" in word else []
    candidates = [word]
    if word.startswith("@") and len(word) > 1:
        candidates.append(word[1:])
    return candidates


@dataclass(frozen=True)
class ExactCommand:
    """The whole command is exactly one of the listed commands (one plain segment)."""

    commands: tuple[tuple[str, ...], ...]
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        command = _command(subject)
        if command.error is not None or len(command.segments) != 1:
            return False
        segment = command.segments[0]
        if segment.redirects or segment.wrappers or segment.assignments:
            return False
        return segment.argv in self.commands


@dataclass(frozen=True)
class PackageInstall:
    """A package-manager install (from the rulebook's table), including ``python -m pip``."""

    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        for segment in _command(subject).segments:
            program, args = segment.program, list(segment.args)
            if program == "python" and "-m" in args:
                module_at = args.index("-m") + 1
                if module_at < len(args) and normalize_program(args[module_at]) == "pip":
                    program, args = "pip", args[module_at + 1 :]
            subcommands = ctx.package_managers.get(program)
            if subcommands is None:
                continue
            positional = tuple(arg for arg in args if not arg.startswith("-"))
            if any(positional[: len(sub)] == sub for sub in subcommands):
                return True
        return False


@dataclass(frozen=True)
class AllProgramsIn:
    """Every segment is a listed program, run directly (no wrapper)."""

    programs: frozenset[str]
    tools: ClassVar[frozenset[str]] = COMMAND_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        command = _command(subject)
        return (
            command.error is None
            and bool(command.segments)
            and all(
                not segment.wrappers and segment.program in self.programs
                for segment in command.segments
            )
        )


def _command(subject: Subject) -> ParsedCommand:
    if subject.command is None:
        raise ValueError("a command matcher was asked about an action that is not a command")
    return subject.command


# --- web fetch -----------------------------------------------------------------------


@dataclass(frozen=True)
class UrlSchemeNotIn:
    schemes: frozenset[str]
    tools: ClassVar[frozenset[str]] = FETCH_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        try:
            scheme = urlsplit(subject.action.url).scheme.lower()
        except ValueError:
            return True
        return scheme not in self.schemes


@dataclass(frozen=True)
class UrlHostIn:
    """The URL's host is a listed host or a subdomain of one.

    Only plain URLs qualify: no credentials, no backslashes, whitespace or control
    characters, and a host made of letters, digits, dots and hyphens. Anything else could
    be read differently by the fetcher than by this check.
    """

    hosts: tuple[str, ...]
    tools: ClassVar[frozenset[str]] = FETCH_TOOLS

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        host = _plain_host(subject.action.url)
        if host is None:
            return False
        return any(host == allowed or host.endswith("." + allowed) for allowed in self.hosts)


def _plain_host(url: str) -> str | None:
    if any(char.isspace() or ord(char) < 0x20 or char in "\\\x7f" for char in url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or not host:
        return None
    host = host.rstrip(".")
    return host if _HOST.fullmatch(host) else None


# --- registry ------------------------------------------------------------------------

_Factory = Callable[[str, object], Matcher]


def _bool(cls: Callable[[], Matcher]) -> _Factory:
    def build(name: str, param: object) -> Matcher:
        _expect_true(name, param)
        return cls()

    return build


def _programs(cls: Callable[[frozenset[str]], Matcher]) -> _Factory:
    def build(name: str, param: object) -> Matcher:
        return cls(frozenset(_expect_strings(name, param)))

    return build


def _path_globs(name: str, param: object) -> Matcher:
    return PathGlob(_expect_strings(name, param))


def _exact(name: str, param: object) -> Matcher:
    return ExactCommand(tuple(tuple(item.split()) for item in _expect_strings(name, param)))


def _bulk(name: str, param: object) -> Matcher:
    return BulkDelete(_expect_count(name, param))


def _schemes(name: str, param: object) -> Matcher:
    return UrlSchemeNotIn(frozenset(item.lower() for item in _expect_strings(name, param)))


def _hosts(name: str, param: object) -> Matcher:
    hosts = tuple(item.lower().rstrip(".") for item in _expect_strings(name, param))
    for host in hosts:
        if not _HOST.fullmatch(host):
            raise ValueError(f"'{name}' has an invalid host name: {host!r}")
    return UrlHostIn(hosts)


MATCHERS: Final[dict[str, _Factory]] = {
    "always": _bool(Always),
    "path_outside_workspace": _bool(PathOutsideWorkspace),
    "path_secret": _bool(PathSecret),
    "path_glob": _path_globs,
    "path_is_test": _bool(PathIsTest),
    "parse_error": _bool(ParseError),
    "program_in": _programs(ProgramIn),
    "pipe_to": _programs(PipeTo),
    "program_with_redirect": _programs(ProgramWithRedirect),
    "chmod_world_writable": _bool(ChmodWorldWritable),
    "delete_outside_workspace": _bool(DeleteOutsideWorkspace),
    "bulk_delete": _bulk,
    "secret_reference": _bool(SecretReference),
    "arg_outside_workspace": _bool(ArgOutsideWorkspace),
    "exact_command": _exact,
    "package_install": _bool(PackageInstall),
    "all_programs_in": _programs(AllProgramsIn),
    "url_scheme_not_in": _schemes,
    "url_host_in": _hosts,
}


def build_matcher(name: str, param: object) -> Matcher:
    """Build a named matcher; raises ValueError for an unknown name or a bad parameter."""
    factory = MATCHERS.get(name)
    if factory is None:
        raise ValueError(f"unknown matcher '{name}'")
    return factory(name, param)
