"""Replace secrets with typed placeholders before text reaches a model, a log or a person.

Masking is pattern based, so it is the second wall: the first is that secret files are
denied by the rulebook and never handed to the sandbox. A secret in an unknown format is
not caught here.

Passes run in a fixed order, each one skipping text an earlier pass already masked:

1. whole private-key blocks (an unterminated block masks to the end of the text);
2. for env files only, the value of every ``NAME=value`` line;
3. values assigned to secret-named variables (``password = "..."``, ``API_KEY: ...``);
4. known token formats (provider keys, JWTs, bearer tokens);
5. email addresses.
"""

from __future__ import annotations

import itertools
import re
from typing import Final, TypeAlias

JsonValue: TypeAlias = "str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None"

PLACEHOLDER_PATTERN: Final = re.compile(r"\[MASKED:[a-z_]+\]")

PRIVATE_KEY: Final = "private_key"
ENV_VALUE: Final = "env_value"
SECRET_VALUE: Final = "secret_value"  # noqa: S105 - a kind name
BEARER_TOKEN: Final = "bearer_token"  # noqa: S105 - a kind name
EMAIL: Final = "email"

_NOT_ALNUM_BEFORE = r"(?<![A-Za-z0-9])"

PRIVATE_KEY_BLOCK: Final = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r".*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL,
)

# Ordered: the Anthropic format must run before the broader OpenAI one ("sk-ant-..." is
# also a valid "sk-..." string).
TOKEN_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("anthropic_key", re.compile(_NOT_ALNUM_BEFORE + r"sk-ant-[A-Za-z0-9_-]{20,}")),
    (
        "openai_key",
        # Project, service-account and admin keys have a distinctive prefix. A bare
        # "sk-" string must also contain a digit, so prose like "sk-learn-..." survives.
        re.compile(
            _NOT_ALNUM_BEFORE + r"sk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
            r"|(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,})"
        ),
    ),
    (
        "stripe_key",
        re.compile(
            _NOT_ALNUM_BEFORE + r"(?:(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"
            r"|whsec_[A-Za-z0-9]{20,})"
        ),
    ),
    ("groq_key", re.compile(_NOT_ALNUM_BEFORE + r"gsk_[A-Za-z0-9]{20,}")),
    (
        "aws_access_key",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}"),
    ),
    (
        "github_token",
        re.compile(
            _NOT_ALNUM_BEFORE + r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"
        ),
    ),
    ("slack_token", re.compile(_NOT_ALNUM_BEFORE + r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(_NOT_ALNUM_BEFORE + r"AIza[0-9A-Za-z_-]{35}")),
    ("huggingface_token", re.compile(_NOT_ALNUM_BEFORE + r"hf_[A-Za-z0-9]{30,}")),
    (
        "jwt",
        re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    ),
)

# "Bearer <token>": the word stays, the token goes. "[" is not a token character, so an
# already masked token is left alone.
BEARER: Final = re.compile(r"(?i)\b(bearer)([ \t]+)[A-Za-z0-9._~+/-]{8,}=*")

EMAIL_ADDRESS: Final = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

_ENV_LINE: Final = re.compile(
    r"^(?P<lead>[ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_.-]*[ \t]*=[ \t]*)"
    r"(?P<value>\S.*?)[ \t]*$",
    re.MULTILINE,
)

# NAME <op> "value" anywhere: keyword arguments, dict and JSON entries, assignments.
_QUOTED_ASSIGNMENT: Final = re.compile(
    r"(?P<lead>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=lead)"
    r"(?P<sep>[ \t]*(?::=|=>|==|=|:)[ \t]*)"
    r"(?:\"(?P<dq>[^\"\n]*)\"|'(?P<sq>[^'\n]*)')"
)

# A whole line NAME = value / NAME: value with an unquoted value (env dumps, YAML, INI).
_UNQUOTED_ASSIGNMENT: Final = re.compile(
    r"^(?P<lead>[ \t]*(?:export[ \t]+)?[\"']?(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)[\"']?"
    r"[ \t]*[=:][ \t]*)"
    r"(?P<value>[^\s'\"`(){}\[\]<>,;#]+)"
    r"(?P<rest>[ \t]*(?:#.*)?)$",
    re.MULTILINE,
)

_SECRET_WORDS: Final = frozenset(
    {
        "secret",
        "secrets",
        "token",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "apikey",
        "auth",
    }
)
_SECRET_WORD_PAIRS: Final = frozenset(
    {
        ("api", "key"),
        ("private", "key"),
        ("access", "key"),
        ("secret", "key"),
        ("signing", "key"),
        ("encryption", "key"),
        ("client", "secret"),
        ("access", "token"),
    }
)
_NON_SECRET_LITERALS: Final = frozenset(
    {"none", "null", "nil", "true", "false", "yes", "no", "on", "off", "~", "changeme"}
)
_DOTTED_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_NUMBER: Final = re.compile(r"[0-9][0-9_.]*")
_CAMEL_BOUNDARY: Final = re.compile(r"([a-z0-9])([A-Z])")
_WORD_SPLIT: Final = re.compile(r"[^a-z0-9]+")

# Unquoted assignments in source code are expressions, not values, so they are skipped there.
_CODE_SUFFIXES: Final = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".php",
    ".swift",
    ".scala",
)

_MIN_QUOTED_SECRET = 4
_MIN_UNQUOTED_SECRET = 4


def placeholder(kind: str) -> str:
    """The typed placeholder that replaces a secret of the given kind."""
    return f"[MASKED:{kind}]"


def is_env_file(name: str) -> bool:
    """True for ``.env``, ``.env.<anything>`` and ``<anything>.env`` (by base name)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base == ".env" or base.startswith(".env.") or base.endswith(".env")


def is_secret_name(name: str) -> bool:
    """True when a variable or key name says it holds a secret (``DB_PASSWORD``, ``apiKey``).

    Words are matched whole, so ``author`` or ``tokenizer`` are not secret names.
    """
    words = _WORD_SPLIT.split(_CAMEL_BOUNDARY.sub(r"\1_\2", name).lower())
    words = [word for word in words if word]
    if any(word in _SECRET_WORDS for word in words):
        return True
    return any(pair in _SECRET_WORD_PAIRS for pair in itertools.pairwise(words))


def mask(text: str, source: str | None = None) -> str:
    """Return ``text`` with every recognised secret replaced by ``[MASKED:<kind>]``.

    ``source`` is the file or URL the text came from, when known. Env files have every
    value masked; source files skip the unquoted-assignment pass.
    """
    text = PRIVATE_KEY_BLOCK.sub(placeholder(PRIVATE_KEY), text)
    if source is not None and is_env_file(source):
        text = _ENV_LINE.sub(_mask_env_line, text)
    text = _QUOTED_ASSIGNMENT.sub(_mask_quoted_assignment, text)
    if source is None or not source.lower().endswith(_CODE_SUFFIXES):
        text = _UNQUOTED_ASSIGNMENT.sub(_mask_unquoted_assignment, text)
    for kind, pattern in TOKEN_PATTERNS:
        text = pattern.sub(placeholder(kind), text)
    text = BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{placeholder(BEARER_TOKEN)}", text)
    return EMAIL_ADDRESS.sub(placeholder(EMAIL), text)


def mask_value(value: JsonValue) -> JsonValue:
    """Mask every string in nested JSON-like data (keys included)."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    if isinstance(value, dict):
        return {mask(key): mask_value(item) for key, item in value.items()}
    return value


def contains_placeholder(text: str) -> bool:
    """True when ``text`` holds a masking placeholder."""
    return PLACEHOLDER_PATTERN.search(text) is not None


def _is_placeholder(value: str) -> bool:
    return PLACEHOLDER_PATTERN.fullmatch(value) is not None


def _mask_env_line(match: re.Match[str]) -> str:
    if _is_placeholder(match.group("value")):
        return match.group(0)
    return match.group("lead") + placeholder(ENV_VALUE)


def _mask_quoted_assignment(match: re.Match[str]) -> str:
    double = match.group("dq")
    value = double if double is not None else match.group("sq")
    if (
        not is_secret_name(match.group("name"))
        or len(value) < _MIN_QUOTED_SECRET
        or _is_placeholder(value)
    ):
        return match.group(0)
    quote = '"' if double is not None else "'"
    lead = match.group("lead")
    masked_value = f"{quote}{placeholder(SECRET_VALUE)}{quote}"
    return f"{lead}{match.group('name')}{lead}{match.group('sep')}{masked_value}"


def _mask_unquoted_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    if not is_secret_name(match.group("name")) or not _looks_like_secret_literal(value):
        return match.group(0)
    return match.group("lead") + placeholder(SECRET_VALUE) + match.group("rest")


def _looks_like_secret_literal(value: str) -> bool:
    """An unquoted value that is a literal, not a number, keyword or reference."""
    return not (
        len(value) < _MIN_UNQUOTED_SECRET
        or value.lower() in _NON_SECRET_LITERALS
        or value.startswith(("$", "%"))
        or _NUMBER.fullmatch(value) is not None
        or _DOTTED_NAME.fullmatch(value) is not None
    )
