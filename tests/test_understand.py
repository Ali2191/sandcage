"""Understand: task only, cleaned and capped, never a gate, one retry, masked answers."""

from __future__ import annotations

import json

import pytest

from sandcage.agent.llm import Completion, Message, ModelError
from sandcage.agent.scenarios import CLARIFY_QUESTION, DEFAULT_TASK, scripted_models
from sandcage.agent.scripted import ScriptedClient
from sandcage.contract.approval import question_approval
from sandcage.roles.common import ReplyError
from sandcage.roles.understand import (
    MAX_ANSWER,
    Understanding,
    effective_criteria,
    parse_understanding,
    understand,
)
from tests.secrets_fixtures import GITHUB_TOKEN


def _reply(**data: object) -> str:
    return json.dumps({"task": "Fix the discount", "done_criteria": [], "question": None} | data)


def test_the_scripted_replies_parse() -> None:
    plain = understand(scripted_models("demo").understand, DEFAULT_TASK)
    assert plain.valid
    assert plain.question is None
    assert plain.done_criteria == ("The failing discount test passes", "No other test breaks")
    asking = understand(scripted_models("clarify").understand, DEFAULT_TASK)
    assert asking.question == CLARIFY_QUESTION


def test_caps_masks_and_drops_empties() -> None:
    found = parse_understanding(
        _reply(
            task=f"Fix it {GITHUB_TOKEN} " + "t" * 600,
            done_criteria=["", "  ", "c" * 300, 5, *[f"crit {i}" for i in range(10)]],
            question="  ",
        )
    )
    assert GITHUB_TOKEN not in found.task
    assert len(found.task) == 500
    assert found.done_criteria[0] == "c" * 200
    assert len(found.done_criteria) == 8
    assert found.question is None  # a blank question is no question
    assert len(parse_understanding(_reply(question="q" * 400)).question or "") == 300


@pytest.mark.parametrize(
    "reply",
    [
        "no json",
        _reply(task=""),
        _reply(task=5),
        _reply(done_criteria="one"),
        _reply(question=True),
        _reply(question=["a"]),
    ],
)
def test_rejects_bad_shapes(reply: str) -> None:
    with pytest.raises(ReplyError):
        parse_understanding(reply)


def test_sees_only_the_task() -> None:
    client = ScriptedClient([_reply()])
    understand(client, f"Fix the bug {GITHUB_TOKEN}")
    assert len(client.calls) == 1
    seen = client.seen
    assert "Fix the bug" in seen
    assert GITHUB_TOKEN not in seen
    assert "README" not in seen  # no repository content
    assert "invoice" not in seen


def test_one_retry_then_fallback_to_the_task_as_given() -> None:
    client = ScriptedClient(["nope", _reply(task="Restated")])
    assert understand(client, "Do it").task == "Restated"
    assert "could not be used" in client.calls[1][-1]["content"]

    fallback = understand(ScriptedClient(["nope", "still nope"]), f"Do it {GITHUB_TOKEN}")
    assert (fallback.valid, fallback.done_criteria, fallback.question) == (False, (), None)
    assert fallback.task.startswith("Do it")
    assert GITHUB_TOKEN not in fallback.task
    assert fallback.problem is not None


def test_a_call_error_is_not_a_gate() -> None:
    class Broken:
        def complete(self, messages: list[Message]) -> Completion:
            raise ModelError("down")

    found = understand(Broken(), "Do it")  # type: ignore[arg-type]
    assert (found.valid, found.task) == (False, "Do it")


def test_the_answer_is_masked_capped_and_reaches_the_brief() -> None:
    asking = Understanding("Fix the discount", question=CLARIFY_QUESTION)
    answered = asking.answered(f"A percentage. {GITHUB_TOKEN} " + "x" * 2000)
    assert answered.answer is not None
    assert len(answered.answer) == MAX_ANSWER
    assert GITHUB_TOKEN not in answered.answer
    assert "Answer: A percentage." in answered.brief()
    assert CLARIFY_QUESTION in answered.brief()
    assert Understanding("Fix it").brief() == "Fix it"
    for empty in (None, "", "   \x00"):
        with pytest.raises(ValueError, match="not answered"):
            asking.answered(empty)
    approval = question_approval(CLARIFY_QUESTION)
    assert approval.resolve("answer", "A percentage").answer == "A percentage"


def test_user_criteria_take_precedence() -> None:
    understood = Understanding("t", done_criteria=("from understand",))
    assert effective_criteria(["mine", " "], understood) == ["mine"]
    assert effective_criteria([], understood) == ["from understand"]
    assert effective_criteria([], Understanding("t")) == []  # left to the planner
