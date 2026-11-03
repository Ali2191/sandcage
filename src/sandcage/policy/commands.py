"""Turn a shell command string into simple commands (segments) the rulebook can judge.

A small POSIX-shell lexer, written for policy rather than execution:

- splits on ``;``, ``&&``, ``||``, ``&``, ``|``, ``|&``, newlines and subshell parentheses,
  and records which segments read a pipe;
- records redirects (file-descriptor prefixes such as ``2>`` are dropped);
- strips leading assignments, shell keywords and wrapper programs (``sudo``, ``env``,
  ``timeout``, ``xargs`` ...) so ``sudo curl`` is judged as ``curl``;
- recurses into ``sh -c``, ``eval``, ``$(...)``, backticks, process substitution and
  ``find -exec`` up to :data:`MAX_DEPTH` levels, so a program cannot hide inside them;
- normalises program names (``/usr/bin/python3.11`` becomes ``python``).

Parsing never raises. Anything it cannot read with certainty (unbalanced quotes, a
redirect without a target, ANSI-C ``$'...'`` strings, nesting deeper than the limit)
sets :attr:`ParsedCommand.error`, which the rulebook turns into a human question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

MAX_DEPTH: Final = 3
MAX_COMMAND_LENGTH: Final = 20_000

SHELLS: Final = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "ash", "csh", "tcsh"}
)

# Programs that run another program given in their arguments, with the flags that take a
# separate value and the number of positional arguments before the wrapped command.
WRAPPERS: Final[dict[str, tuple[frozenset[str], int]]] = {
    "sudo": (frozenset({"-u", "-g", "-h", "-p", "-C", "-U", "-D", "-r", "-t", "-T"}), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "env": (frozenset({"-u", "-C", "--unset", "--chdir"}), 0),
    "nohup": (frozenset(), 0),
    "time": (frozenset({"-f", "-o"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "ionice": (frozenset({"-c", "-n", "-p", "-P", "-u"}), 0),
    "xargs": (
        frozenset({"-I", "-d", "-E", "-L", "-n", "-P", "-s", "-a", "--arg-file", "--delimiter"}),
        0,
    ),
    "timeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    "stdbuf": (frozenset({"-i", "-o", "-e"}), 0),
    "setsid": (frozenset(), 0),
    "unbuffer": (frozenset(), 0),
    "busybox": (frozenset(), 0),
    "strace": (frozenset({"-o", "-e", "-p", "-s", "-u", "-P"}), 0),
    "ltrace": (frozenset({"-o", "-e", "-p", "-s", "-u"}), 0),
    "chroot": (frozenset({"--userspec", "--groups"}), 1),
    "flock": (frozenset({"-w", "-E", "-c"}), 1),
    "watch": (frozenset({"-n", "--interval"}), 0),
    "caffeinate": (frozenset({"-t", "-w"}), 0),
}

# Shell words that are not commands when they start a segment.
_KEYWORDS: Final = frozenset(
    {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time"}
)
_FIND_EXEC_FLAGS: Final = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

_SEPARATOR_OPS: Final = frozenset({";", ";;", "&&", "||", "&", "|", "|&", "(", ")"})
_PIPE_OPS: Final = frozenset({"|", "|&"})
# Longest first, so "<<<" is not read as "<<" then "<".
_REDIRECT_OPS: Final = ("<<<", "<<-", "&>>", "<<", "<>", "<&", ">>", ">&", ">|", "&>", "<", ">")
_WRITE_OPS: Final = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})

_ASSIGNMENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_PYTHON: Final = re.compile(r"python[0-9]*(?:\.[0-9]+)*")
_PIP: Final = re.compile(r"pip[0-9]*(?:\.[0-9]+)*")
_FD: Final = re.compile(r"[0-9]+|-")
_SUBSTITUTION_TEXT: Final = "$(...)"


@dataclass(frozen=True)
class Redirect:
    """One redirection: the operator as written and its target word."""

    op: str
    target: str

    @property
    def is_fd_dup(self) -> bool:
        """``2>&1``-style duplication of a file descriptor (no file involved)."""
        return self.op in {">&", "<&"} and _FD.fullmatch(self.target) is not None

    @property
    def writes(self) -> bool:
        """True when the redirect writes to a file."""
        return self.op in _WRITE_OPS and not self.is_fd_dup


@dataclass(frozen=True)
class Segment:
    """One simple command.

    ``argv`` is the command as it will run (after assignments, keywords and wrappers are
    stripped); ``program`` is its normalised first word. ``candidates`` lists every word a
    wrapper might run, normalised, so deny rules can look past wrapper options this
    parser does not know.
    """

    program: str
    argv: tuple[str, ...]
    redirects: tuple[Redirect, ...]
    piped: bool
    wrappers: tuple[str, ...]
    assignments: tuple[str, ...]
    candidates: tuple[str, ...]
    depth: int

    @property
    def args(self) -> tuple[str, ...]:
        return self.argv[1:]

    @property
    def words(self) -> tuple[str, ...]:
        """The normalised program followed by its arguments."""
        return (self.program, *self.args) if self.argv else ()

    @property
    def possible_programs(self) -> frozenset[str]:
        """The program plus anything a wrapper might run."""
        programs = set(self.candidates)
        if self.program:
            programs.add(self.program)
        return frozenset(programs)


@dataclass(frozen=True)
class ParsedCommand:
    raw: str
    segments: tuple[Segment, ...]
    error: str | None

    @property
    def has_redirect(self) -> bool:
        return any(segment.redirects for segment in self.segments)

    @property
    def programs(self) -> frozenset[str]:
        """Every program that may run, wrapped ones included."""
        found: set[str] = set()
        for segment in self.segments:
            found |= segment.possible_programs
        return frozenset(found)


def parse_command(command: str) -> ParsedCommand:
    """Parse ``command``; never raises (problems are reported in ``error``)."""
    collected: list[Segment] = []
    try:
        if not command.strip():
            raise _ParseError("the command is empty")
        if len(command) > MAX_COMMAND_LENGTH:
            raise _ParseError(f"the command is longer than {MAX_COMMAND_LENGTH} characters")
        _parse_into(command, 0, collected)
    except _ParseError as problem:
        return ParsedCommand(raw=command, segments=tuple(collected), error=str(problem))
    return ParsedCommand(raw=command, segments=tuple(collected), error=None)


def normalize_program(word: str) -> str:
    """``/usr/bin/python3.11`` → ``python``, ``pip3`` → ``pip``, ``py.test`` → ``pytest``."""
    name = word.rstrip("/").rsplit("/", 1)[-1] or word
    if _PYTHON.fullmatch(name):
        return "python"
    if _PIP.fullmatch(name):
        return "pip"
    if name == "py.test":
        return "pytest"
    return name


# --- lexer -----------------------------------------------------------------------------


class _ParseError(Exception):
    pass


@dataclass(frozen=True)
class _Word:
    text: str
    substitutions: tuple[str, ...]


@dataclass(frozen=True)
class _Op:
    op: str


_Token = _Word | _Op


class _Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.tokens: list[_Token] = []
        self._chars: list[str] = []
        self._subs: list[str] = []
        self._in_word = False

    def run(self) -> list[_Token]:
        text = self.text
        while self.pos < len(text):
            char = text[self.pos]
            if char in " \t\r":
                self._flush()
                self.pos += 1
            elif char == "\n":
                self._flush()
                self.tokens.append(_Op(";"))
                self.pos += 1
            elif char == "#" and not self._in_word:
                end = text.find("\n", self.pos)
                self.pos = len(text) if end == -1 else end
            elif char == "\\":
                self._backslash()
            elif char == "'":
                self._single_quoted()
            elif char == '"':
                self.pos += 1
                self._double_quoted()
            elif char == "`":
                self._add_substitution(self._backtick_body())
            elif char == "$":
                self._dollar()
            elif char in "<>":
                self._angle(char)
            elif char in "|&;()":
                self._operator(char)
            else:
                self._chars.append(char)
                self._in_word = True
                self.pos += 1
        self._flush()
        return self.tokens

    def _flush(self) -> None:
        if self._in_word:
            self.tokens.append(_Word("".join(self._chars), tuple(self._subs)))
        self._chars = []
        self._subs = []
        self._in_word = False

    def _add_substitution(self, body: str) -> None:
        self._subs.append(body)
        self._chars.append(_SUBSTITUTION_TEXT)
        self._in_word = True

    def _backslash(self) -> None:
        nxt = self.text[self.pos + 1 : self.pos + 2]
        if nxt == "":
            raise _ParseError("the command ends with a backslash")
        if nxt != "\n":
            self._chars.append(nxt)
            self._in_word = True
        self.pos += 2

    def _single_quoted(self) -> None:
        end = self.text.find("'", self.pos + 1)
        if end == -1:
            raise _ParseError("a single quote is not closed")
        self._chars.append(self.text[self.pos + 1 : end])
        self._in_word = True
        self.pos = end + 1

    def _double_quoted(self) -> None:
        """Read a double-quoted string; ``self.pos`` is just after the opening quote."""
        text = self.text
        self._in_word = True
        while self.pos < len(text):
            char = text[self.pos]
            if char == '"':
                self.pos += 1
                return
            if char == "\\":
                nxt = text[self.pos + 1 : self.pos + 2]
                if nxt in {"$", "`", '"', "\\"}:
                    self._chars.append(nxt)
                elif nxt != "\n":
                    self._chars.append("\\" + nxt)
                self.pos += 2
            elif char == "`":
                self._add_substitution(self._backtick_body())
            elif char == "$" and text[self.pos + 1 : self.pos + 2] in {"(", "{"}:
                self._dollar()
            else:
                self._chars.append(char)
                self.pos += 1
        raise _ParseError("a double quote is not closed")

    def _backtick_body(self) -> str:
        text = self.text
        index = self.pos + 1
        body: list[str] = []
        while index < len(text):
            char = text[index]
            if char == "\\" and text[index + 1 : index + 2] in {"`", "\\", "$"}:
                body.append(text[index + 1])
                index += 2
            elif char == "`":
                self.pos = index + 1
                return "".join(body)
            else:
                body.append(char)
                index += 1
        raise _ParseError("a backtick is not closed")

    def _dollar(self) -> None:
        nxt = self.text[self.pos + 1 : self.pos + 2]
        if nxt == "(":
            body, end = _balanced(self.text, self.pos + 1)
            self._add_substitution(body)
            self.pos = end
        elif nxt == "{":
            body, end = _balanced(self.text, self.pos + 1, open_char="{", close_char="}")
            if "$(" in body or "`" in body:
                raise _ParseError("a command substitution is hidden in a parameter expansion")
            self._chars.append("${" + body + "}")
            self._in_word = True
            self.pos = end
        elif nxt == "'":
            raise _ParseError("ANSI-C quoted strings ($'...') are not supported")
        elif nxt == '"':
            self.pos += 2
            self._double_quoted()
        else:
            self._chars.append("$")
            self._in_word = True
            self.pos += 1

    def _angle(self, char: str) -> None:
        if self.text[self.pos + 1 : self.pos + 2] == "(":
            body, end = _balanced(self.text, self.pos + 1)
            self._add_substitution(body)
            self.pos = end
            return
        if self._in_word and not self._subs and self._chars and "".join(self._chars).isdigit():
            # A file-descriptor prefix ("2>"): not an argument, dropped.
            self._chars = []
            self._in_word = False
        self._flush()
        for op in _REDIRECT_OPS:
            if self.text.startswith(op, self.pos):
                self.tokens.append(_Op(op))
                self.pos += len(op)
                return
        raise AssertionError(f"unreachable: {char!r} always starts a redirect")

    def _operator(self, char: str) -> None:
        self._flush()
        text = self.text
        if char == "&" and text.startswith(("&>>", "&>"), self.pos):
            op = "&>>" if text.startswith("&>>", self.pos) else "&>"
        else:
            pair = text[self.pos : self.pos + 2]
            op = pair if pair in {"&&", "||", "|&", ";;"} else char
        self.tokens.append(_Op(op))
        self.pos += len(op)


def _balanced(
    text: str, start: int, open_char: str = "(", close_char: str = ")"
) -> tuple[str, int]:
    """Read from ``text[start] == open_char`` to its matching close; return (body, end)."""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "'":
            close = text.find("'", index + 1)
            if close == -1:
                raise _ParseError("a single quote is not closed")
            index = close + 1
            continue
        if char == '"':
            index = _skip_double_quoted(text, index + 1)
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
        index += 1
    raise _ParseError(f"a '{open_char}' is not closed")


def _skip_double_quoted(text: str, index: int) -> int:
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == '"':
            return index + 1
        else:
            index += 1
    raise _ParseError("a double quote is not closed")


# --- segments --------------------------------------------------------------------------


def _parse_into(command: str, depth: int, out: list[Segment]) -> None:
    if depth > MAX_DEPTH:
        raise _ParseError(f"commands are nested more than {MAX_DEPTH} levels deep")
    lexer = _Lexer(command)
    lex_error: _ParseError | None = None
    try:
        tokens = lexer.run()
    except _ParseError as problem:
        # Keep what was read before the problem, so deny rules still see those programs.
        lex_error = problem
        tokens = lexer.tokens
    words: list[_Word] = []
    redirects: list[Redirect] = []
    piped = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, _Word):
            words.append(token)
        elif token.op in _SEPARATOR_OPS:
            _emit(words, redirects, piped, depth, out)
            words, redirects = [], []
            piped = token.op in _PIPE_OPS
        else:
            target = tokens[index + 1] if index + 1 < len(tokens) else None
            if not isinstance(target, _Word):
                raise _ParseError(f"the redirect '{token.op}' has no target")
            redirects.append(Redirect(token.op, target.text))
            for body in target.substitutions:
                _parse_into(body, depth + 1, out)
            index += 1
        index += 1
    _emit(words, redirects, piped, depth, out)
    if lex_error is not None:
        raise lex_error


def _emit(
    words: list[_Word], redirects: list[Redirect], piped: bool, depth: int, out: list[Segment]
) -> None:
    texts = [word.text for word in words]
    out.extend(_segments_from_words(texts, redirects, piped, depth))
    for word in words:
        for body in word.substitutions:
            _parse_into(body, depth + 1, out)


def _segments_from_words(
    texts: list[str], redirects: list[Redirect], piped: bool, depth: int
) -> list[Segment]:
    """Build the segment for one simple command, plus any it runs (sh -c, eval, find -exec)."""
    position = 0
    assignments: list[str] = []
    while position < len(texts):
        word = texts[position]
        if word in _KEYWORDS:
            position += 1
        elif word == "function":
            position += 2
        elif _ASSIGNMENT.match(word):
            assignments.append(word)
            position += 1
        else:
            break
    rest = texts[position:]
    if not rest and not redirects and not assignments:
        return []
    wrappers, argv = _strip_wrappers(rest)
    candidates = _wrapped_candidates(rest) if wrappers else ()
    program = normalize_program(argv[0]) if argv else ""
    segment = Segment(
        program=program,
        argv=tuple(argv),
        redirects=tuple(redirects),
        piped=piped,
        wrappers=tuple(wrappers),
        assignments=tuple(assignments),
        candidates=candidates,
        depth=depth,
    )
    nested: list[Segment] = []
    if program in SHELLS:
        script = _shell_script(argv[1:])
        if script is not None:
            _parse_into(script, depth + 1, nested)
    elif program == "eval" and len(argv) > 1:
        _parse_into(" ".join(argv[1:]), depth + 1, nested)
    elif program == "find":
        nested.extend(_find_exec_segments(argv[1:], depth + 1))
    if "env" in wrappers:
        script = _env_split_string(rest)
        if script is not None:
            _parse_into(script, depth + 1, nested)
    return [segment, *nested]


def _strip_wrappers(words: list[str]) -> tuple[list[str], list[str]]:
    wrappers: list[str] = []
    index = 0
    while index < len(words) and normalize_program(words[index]) in WRAPPERS:
        name = normalize_program(words[index])
        wrappers.append(name)
        value_flags, positional = WRAPPERS[name]
        index += 1
        while index < len(words):
            word = words[index]
            if word == "--":
                index += 1
                break
            if word.startswith("-") and len(word) > 1:
                index += 1
                if "=" not in word and word in value_flags:
                    index += 1
            elif name == "env" and _ASSIGNMENT.match(word):
                index += 1
            elif positional > 0:
                positional -= 1
                index += 1
            else:
                break
    return wrappers, words[index:]


def _wrapped_candidates(words: list[str]) -> tuple[str, ...]:
    """Every non-flag word after a wrapper, split on spaces and normalised."""
    found: list[str] = []
    for word in words:
        if word.startswith("-"):
            continue
        for part in word.split():
            name = normalize_program(part)
            if name and name not in found:
                found.append(name)
    return tuple(found)


def _shell_script(args: list[str]) -> str | None:
    """The script of ``sh -c SCRIPT`` (also ``-lc``, ``-ec`` ...), else None."""
    for index, arg in enumerate(args):
        if arg == "--":
            return None
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            if index + 1 >= len(args):
                raise _ParseError("a shell '-c' has no command")
            return args[index + 1]
    return None


def _env_split_string(words: list[str]) -> str | None:
    """The command string of ``env -S 'cmd args'``, else None."""
    for index, word in enumerate(words):
        if word in {"-S", "--split-string"}:
            if index + 1 >= len(words):
                raise _ParseError("'env -S' has no command")
            return words[index + 1]
        if word.startswith(("-S", "--split-string=")) and word not in {"-S", "--split-string"}:
            return word.split("=", 1)[1] if word.startswith("--") else word[2:]
    return None


def _find_exec_segments(args: list[str], depth: int) -> list[Segment]:
    if depth > MAX_DEPTH:
        raise _ParseError(f"commands are nested more than {MAX_DEPTH} levels deep")
    segments: list[Segment] = []
    index = 0
    while index < len(args):
        if args[index] in _FIND_EXEC_FLAGS:
            end = index + 1
            while end < len(args) and args[end] not in {";", "+"}:
                end += 1
            segments.extend(_segments_from_words(args[index + 1 : end], [], False, depth))
            index = end
        index += 1
    return segments
