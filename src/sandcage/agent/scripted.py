"""The scripted chat client: replays fixed replies and records every message list it was
sent, so tests can assert on exactly what a model saw. Deliberately dead simple."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field

from sandcage.agent.llm import Completion, Message, ModelError, Usage


@dataclass
class ScriptedClient:
    replies: Sequence[str]
    when_done: str | None = None
    usage: Usage | None = None
    calls: list[list[Message]] = field(default_factory=list)

    def complete(self, messages: Sequence[Message]) -> Completion:
        self.calls.append(copy.deepcopy(list(messages)))
        index = len(self.calls) - 1
        if index < len(self.replies):
            return Completion(self.replies[index], self.usage)
        if self.when_done is not None:
            return Completion(self.when_done, self.usage)
        raise ModelError("the scripted model has no more replies")

    @property
    def seen(self) -> str:
        """Everything this model was ever sent, as one text (for "never saw X" checks)."""
        return "\n".join(m["content"] for call in self.calls for m in call)
