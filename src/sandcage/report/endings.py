"""The one wording catalogue for how a run ended: pill label and headline for every status.

The backend owns this wording; the panel shows ``outcome`` and ``headline`` exactly as
written and keeps no wording logic of its own. Rules, decided 4 Nov (S8 step 0):

- Only a run that **finished and verified** with a cleared red-team gate is "Delivered".
- A run that did not finish never says "the task got done" and never says "Delivered".
- When an unfinished run left changes behind, those changes are **withheld** (a human can
  still release them); the pill then reads "Withheld" and the headline gives the reason.
- Without a denial the headline never says "blocked" or "cross the line"; asks a human did
  not approve are counted elsewhere (``declined_line``).

``tests/test_endings.py`` pins every string below on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sandcage.contract.events import RunStatus

OUTCOME_DELIVERED: Final = "Delivered"
OUTCOME_WITHHELD: Final = "Withheld"
OUTCOME_STOPPED: Final = "Stopped by you"

NOTHING_DELIVERED: Final = "Nothing is delivered."


@dataclass(frozen=True)
class Ending:
    """How one end status reads. ``plain`` is the headline when nothing is held back;
    ``withheld`` is the headline when changes were made and are held until a human reviews
    them (``None`` for statuses that can never leave changes behind)."""

    label: str
    plain: str
    withheld: str | None = None


def _unfinished(label: str, reason: str) -> Ending:
    return Ending(
        label,
        f"{reason}. {NOTHING_DELIVERED}",
        f"{reason}, so the changes are withheld until you review them.",
    )


ENDINGS: Final[dict[RunStatus, Ending]] = {
    RunStatus.RUNNING: Ending("Running", "The run is still going."),
    RunStatus.FINISHED: Ending(
        OUTCOME_DELIVERED, "The run finished inside the cage, and the task got done."
    ),
    RunStatus.VERIFY_FAILED: Ending(
        "Not verified",
        f"The agent stopped, but the checks did not pass. {NOTHING_DELIVERED}",
        "The agent stopped, but the checks did not pass, so the changes are withheld "
        "until you review them.",
    ),
    RunStatus.NEEDS_CLARIFICATION: Ending(
        "Needs an answer",
        "The agent needed an answer from you and did not get one, so nothing ran.",
    ),
    RunStatus.PLAN_INVALID: Ending(
        "No safe plan",
        "Sandcage could not make a plan it could lock for this task, so nothing ran.",
    ),
    RunStatus.PLAN_REJECTED: Ending("Plan rejected", "You rejected the plan, so nothing ran."),
    RunStatus.BUDGET_STEPS: _unfinished(
        "Out of steps", "The agent used all its steps before the task was done"
    ),
    RunStatus.BUDGET_TOKENS: _unfinished(
        "Out of tokens", "The run used its token limit before the task was done"
    ),
    RunStatus.BUDGET_TIME: _unfinished(
        "Out of time", "The run used its time limit before the task was done"
    ),
    RunStatus.LOOP_REPEAT: _unfinished(
        "Stuck in a loop", "The agent kept repeating the same action, so Sandcage ended the run"
    ),
    RunStatus.KILLED: _unfinished(
        "Ended at the breaker", "The run ended at the breaker before the task was done"
    ),
    RunStatus.INVALID_REPLY: _unfinished(
        "Model gave up", "The model sent replies Sandcage could not use, so the run ended"
    ),
    RunStatus.ERROR: _unfinished("Run failed", "The run failed before the task was done"),
    RunStatus.STOPPED: Ending(OUTCOME_STOPPED, "You stopped this run. Nothing left the cage."),
    RunStatus.INTERRUPTED: _unfinished(
        "Interrupted", "The API restarted while this run was going, so it was cut off"
    ),
}


def ending(status: RunStatus) -> Ending:
    """The catalogue entry for ``status`` (every status has one; a test enforces it)."""
    return ENDINGS[status]
