"""Understand: the first stage. Restate the task, write checkable done criteria and, only
when the task cannot be done at all without it, ask one clarifying question.

The call sees **the task only** (no files, no names, no tools), so no repository content
can steer it. Its reply ``{"task", "done_criteria", "question"}`` is cleaned, masked and
capped. Understand helps the run but is **not a gate**: after an invalid reply twice, or
a failed call, the run continues with the task as given and no criteria (the planner then
writes them), with ``valid`` false.

A question pauses the run for a human (:func:`question_approval`); the answer is capped
at 1000 characters, masked, and joined to the brief every later role sees. An empty or
missing answer stops the run before anything runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from sandcage.agent.llm import ChatClient, Message
from sandcage.masking import mask
from sandcage.plan.manifest import clean
from sandcage.roles.common import ReplyError, ask_json, first_object, text_field, text_list

TASK_LIMIT: Final = 500
CRITERIA: Final = 8
CRITERION_LIMIT: Final = 200
QUESTION_LIMIT: Final = 300
MAX_ANSWER: Final = 1000
INPUT_LIMIT: Final = 4000

SYSTEM_PROMPT: Final = """You read a coding task before any work starts.
Restate it in one or two plain sentences, list up to 8 short done criteria that a test run
or a reviewer can check, and ask one clarifying question ONLY if the task cannot be done
at all without the answer. Most tasks need no question.
Reply with exactly one JSON object and nothing else:
{"task": "<restated task>", "done_criteria": ["..."], "question": "<question>" or null}"""


@dataclass(frozen=True)
class Understanding:
    task: str
    done_criteria: tuple[str, ...] = ()
    question: str | None = None
    valid: bool = True
    problem: str | None = None
    answer: str | None = None
    calls: int = 0

    def brief(self) -> str:
        """What every later role is told about the task."""
        text = self.task
        if self.question and self.answer:
            text += f"\nClarification asked: {self.question}\nAnswer: {self.answer}"
        return text

    def answered(self, answer: str | None) -> Understanding:
        """The human's answer, cleaned, masked and capped; raises ``ValueError`` when
        empty (the run must stop)."""
        text = clean(mask(answer or ""), MAX_ANSWER)
        if not text:
            raise ValueError("the clarifying question was not answered")
        return replace(self, answer=text)


def understand_messages(task: str) -> list[Message]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Task:\n{clean(mask(task), INPUT_LIMIT)}"},
    ]


def parse_understanding(text: str) -> Understanding:
    data = first_object(text)
    restated = text_field(data, "task", TASK_LIMIT, required=True)
    criteria = text_list(data, "done_criteria", items=CRITERIA, limit=CRITERION_LIMIT)
    question_raw = data.get("question")
    if question_raw is not None and not isinstance(question_raw, str):
        raise ReplyError("'question' must be a string or null.")
    question = text_field(data, "question", QUESTION_LIMIT) or None
    return Understanding(restated, tuple(criteria), question)


def understand(client: ChatClient, task: str) -> Understanding:
    asked = ask_json(client, understand_messages(task), parse_understanding)
    if asked.value is None:
        return Understanding(
            clean(mask(task), INPUT_LIMIT), valid=False, problem=asked.problem, calls=asked.calls
        )
    found = asked.value
    return Understanding(found.task, found.done_criteria, found.question, calls=asked.calls)


def effective_criteria(user: Sequence[str], understood: Understanding) -> list[str]:
    """User-supplied criteria win over Understand's; none at all leaves them to the
    planner."""
    given = [clean(mask(item), CRITERION_LIMIT) for item in user]
    given = [item for item in given if item]
    return given[:CRITERIA] if given else list(understood.done_criteria)
