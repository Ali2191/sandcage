"""Every event the harness emits, in every scenario, validates against the committed
``contract/events.schema.json``; the committed sample is the real harness's recording; and
(panel contract request) a blocked step always carries Sandcage's own, non-empty
``blocked_sentence``."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from sandcage.agent.scenarios import ScenarioId, scripted_models
from sandcage.contract.approval import ApprovalKind
from sandcage.contract.events import Event, StepEvent, dump_event
from sandcage.contract.generate import DEFAULT_OUT, SAMPLE_FILE, SCHEMA_FILE
from sandcage.contract.sample import demo_events
from sandcage.harness.pipeline import Harness
from sandcage.sandbox.run import GateMode
from tests.harness_helpers import Run, ScriptedHuman, refuse, run_scenario
from tests.test_harness import Broken

SCHEMA: dict[str, Any] = json.loads((DEFAULT_OUT / SCHEMA_FILE).read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def _stopper(kind: ApprovalKind) -> tuple[ScriptedHuman, Callable[[Harness], None]]:
    holder: dict[str, Harness] = {}
    person = ScriptedHuman()
    person.during[kind] = lambda: holder["h"].request_stop()  # noqa: PLW0108 - bound later
    return person, lambda harness: holder.update(h=harness)


def _variants(tmp_path: Path) -> dict[str, Run]:
    runs: dict[str, Run] = {}
    for scenario in ScenarioId:
        runs[scenario.value] = run_scenario(tmp_path / scenario.value, scenario)
        runs[f"{scenario.value}-refuse-all"] = run_scenario(
            tmp_path / f"{scenario.value}-refuse",
            scenario,
            human=ScriptedHuman(answers=dict.fromkeys(ApprovalKind, refuse)),
        )
        runs[f"{scenario.value}-observe"] = run_scenario(
            tmp_path / f"{scenario.value}-observe", scenario, mode=GateMode.OBSERVE, guard=False
        )
    runs["sneaky-kept-blocked"] = run_scenario(
        tmp_path / "kept",
        "sneaky_urllib",
        human=ScriptedHuman(answers={ApprovalKind.REDTEAM: refuse}),
    )
    runs["sneaky-action-refused"] = run_scenario(
        tmp_path / "action-refused",
        "sneaky_urllib",
        human=ScriptedHuman(answers={ApprovalKind.ACTION: refuse}),
    )
    unreviewed = scripted_models()
    unreviewed.redteam = Broken()  # type: ignore[assignment]
    runs["unreviewed"] = run_scenario(tmp_path / "unreviewed", models=unreviewed)
    for kind in (ApprovalKind.PLAN, ApprovalKind.BREAKER):
        person, keep = _stopper(kind)
        runs[f"stop-at-{kind.value}"] = run_scenario(
            tmp_path / f"stop-{kind.value}", human=person, prepare=keep
        )
    return runs


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Run]:
    return _variants(tmp_path_factory.mktemp("variants"))


def _problems(event: Event) -> list[str]:
    return [error.message for error in VALIDATOR.iter_errors(dump_event(event))]


def test_every_emitted_event_validates_against_the_schema(runs: dict[str, Run]) -> None:
    assert len(runs) == 3 * len(ScenarioId) + 5
    bad = {
        f"{name}#{index} {event.kind}": problems
        for name, run in runs.items()
        for index, event in enumerate(run.result.events)
        if (problems := _problems(event))
    }
    assert bad == {}
    kinds = {event.kind for run in runs.values() for event in run.result.events}
    assert kinds >= {
        "understand", "scout", "plan", "approval_needed", "approval_resolved", "step",
        "checkpoint", "breaker", "verify", "redteam", "deliver", "report", "stopped", "done",
    }  # fmt: skip


def test_every_blocked_step_has_sandcages_sentence(runs: dict[str, Run]) -> None:
    """Panel contract request: ``blocked_sentence`` is never null or empty on a blocked step,
    in any scenario, in the model and in the JSON the panel receives."""
    with_blocks: set[str] = set()
    for name, run in runs.items():
        for event in run.result.events:
            if not isinstance(event, StepEvent) or not event.blocked:
                continue
            with_blocks.add(name)
            sentence = dump_event(event)["blocked_sentence"]
            assert isinstance(sentence, str), f"{name} step {event.step}: {sentence!r}"
            assert sentence.strip(), f"{name} step {event.step}: empty blocked_sentence"
            assert sentence.startswith("Tried to "), f"{name} step {event.step}: {sentence!r}"
    # the check really ran: denials (demo, skip_test) and an ask a human refused
    assert {"demo", "skip_test", "sneaky-action-refused"} <= with_blocks


def test_the_validator_rejects_an_event_that_breaks_the_contract() -> None:
    step = next(e for e in demo_events() if isinstance(e, StepEvent) and e.blocked)
    broken = dump_event(step)
    broken["blocked_sentence"] = 42
    assert list(VALIDATOR.iter_errors(broken))
    fixed = dump_event(step)
    del fixed["executed"]
    assert list(VALIDATOR.iter_errors(fixed))  # a required field missing


def test_the_committed_sample_is_the_real_harness_recording() -> None:
    lines = (DEFAULT_OUT / SAMPLE_FILE).read_text(encoding="utf-8").splitlines()
    recorded = [
        json.dumps(dump_event(e), sort_keys=True, ensure_ascii=False) for e in demo_events()
    ]
    assert lines == recorded
    for line in lines:
        assert not list(VALIDATOR.iter_errors(json.loads(line)))
    first, *_, last = (json.loads(line) for line in lines)
    assert (first["kind"], last["kind"], last["status"]) == ("understand", "done", "finished")
