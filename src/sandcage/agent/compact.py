"""Keep the executor's context under a character budget.

The system message and the task message are always kept whole, and so are the two newest
messages. Older messages are first shortened to stubs (the head of the message, cut before
any untrusted block so a stub never leaves one open, plus "repeat the action to see it
again"); if that is still too long, the oldest are dropped behind one note. The result is
deterministic and never longer than the input.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from sandcage.agent.llm import Message

CONTEXT_BUDGET: Final = 14_000
STUB_LIMIT: Final = 160
KEEP_NEWEST: Final = 2
_PINNED: Final = 2  # system + task
STUB_NOTE: Final = " [shortened; repeat the action to see it again]"
DROPPED_NOTE: Final = "[{count} earlier messages were dropped to keep the context short]"


def size(messages: Sequence[Message]) -> int:
    return sum(len(message["content"]) for message in messages)


def stub(message: Message) -> Message:
    content = message["content"]
    head = content.split("<untrusted", 1)[0].rstrip()
    if len(head) <= STUB_LIMIT and head == content:
        return message
    return {"role": message["role"], "content": head[:STUB_LIMIT] + STUB_NOTE}


def compact(messages: Sequence[Message], budget: int = CONTEXT_BUDGET) -> list[Message]:
    items = list(messages)
    if size(items) <= budget or len(items) <= _PINNED + KEEP_NEWEST:
        return items
    pinned, middle, newest = (
        items[:_PINNED],
        items[_PINNED:-KEEP_NEWEST],
        items[-KEEP_NEWEST:],
    )
    middle = [stub(message) for message in middle]
    dropped = 0
    while middle and size(pinned) + size(middle) + size(newest) + 80 > budget:
        middle.pop(0)
        dropped += 1
    if dropped:
        note: Message = {"role": "user", "content": DROPPED_NOTE.format(count=dropped)}
        middle.insert(0, note)
    return [*pinned, *middle, *newest]
