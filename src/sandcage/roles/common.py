"""What every reviewing role shares: one JSON reply, asked for at most twice.

Model output is untrusted input. Each role has one parse function (validate, clip, cap,
mask) that raises :class:`ReplyError` with a message written for the model; :func:`ask_json`
feeds that message back once and then gives up. A failed call is not retried here (the
client already retries transport errors). The caller decides what "gave up" means, and
every role fails closed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Generic, TypeVar

from pydantic import JsonValue

from sandcage.agent.llm import ChatClient, Message, ModelError
from sandcage.agent.protocol import ActionParseError, extract_json_object
from sandcage.masking import mask
from sandcage.plan.manifest import clean

T = TypeVar("T")

RETRY_ECHO_LIMIT: Final = 4000
_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,79}")


class ReplyError(ValueError):
    """The reply does not fit the role's schema; ``str()`` is the message for the model."""


@dataclass(frozen=True)
class Asked(Generic[T]):
    """The parsed reply, or why there is none. ``calls`` counts model calls made."""

    value: T | None
    problem: str | None
    calls: int

    @property
    def ok(self) -> bool:
        return self.value is not None


def first_object(text: str) -> dict[str, JsonValue]:
    """The first JSON object in a reply (the one shared helper), as a :class:`ReplyError`."""
    try:
        return extract_json_object(text)
    except ActionParseError as problem:
        raise ReplyError(str(problem)) from None


def ask_json(
    client: ChatClient, messages: Sequence[Message], parse: Callable[[str], T]
) -> Asked[T]:
    """Ask, parse, and on an invalid reply ask once more with the problem fed back."""
    conversation = list(messages)
    for attempt in (1, 2):
        try:
            reply = client.complete(conversation).text
        except ModelError as problem:
            return Asked(None, f"the model call failed: {mask(str(problem))}", attempt)
        try:
            return Asked(parse(reply), None, attempt)
        except ReplyError as problem:
            if attempt == 2:
                return Asked(None, f"the reply was unusable twice: {mask(str(problem))}", 2)
            conversation = [
                *conversation,
                {"role": "assistant", "content": mask(reply)[:RETRY_ECHO_LIMIT]},
                {
                    "role": "user",
                    "content": f"That reply could not be used: {mask(str(problem))} "
                    "Reply again with exactly one JSON object in the required shape.",
                },
            ]
    raise AssertionError("unreachable")  # pragma: no cover


# --- small validators shared by the parse functions ----------------------------------------


def text_field(data: dict[str, JsonValue], key: str, limit: int, *, required: bool = False) -> str:
    """A string field: cleaned (no control or direction characters), masked, capped."""
    value = data.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ReplyError(f"'{key}' must be a string.")
    text = clean(mask(value), limit)
    if required and not text:
        raise ReplyError(f"'{key}' must not be empty.")
    return text


def bool_field(data: dict[str, JsonValue], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ReplyError(f"'{key}' must be true or false.")
    return value


def text_list(
    data: dict[str, JsonValue], key: str, *, items: int, limit: int, required: bool = False
) -> list[str]:
    value = data.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ReplyError(f"'{key}' must be a list of strings.")
    cleaned = [clean(mask(item), limit) for item in value if isinstance(item, str)]
    return [item for item in cleaned if item][:items]


def is_identifier(text: str) -> bool:
    return _IDENTIFIER.fullmatch(text) is not None
