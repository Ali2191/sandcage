"""Report wording: only real gate denials count as the agent "crossing the line". An ask a
human did not approve is not an attack; it gets its own honest line and never inflates the
blocked count (decision 4 Nov, S6 step 0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandcage.contract.approval import ApprovalKind
from sandcage.contract.events import RunStatus, StepEvent
from sandcage.report.report import declined_line, headline, is_declined, is_denied
from tests.harness_helpers import Run, ScriptedHuman, refuse, run_scenario

DEMO_HEADLINE = (
    "The agent tried to cross the line. Sandcage blocked it 2 times, and the task still got done."
)


# --- the wording table (a deliberate snapshot: change it only on purpose) --------------------


def test_headline_wording_snapshot() -> None:
    finished, stopped = RunStatus.FINISHED, RunStatus.STOPPED
    assert {
        "finished-0": headline(finished, 0, withheld=False),
        "finished-1": headline(finished, 1, withheld=False),
        "finished-2": headline(finished, 2, withheld=False),
        "withheld-0": headline(finished, 0, withheld=True),
        "withheld-2": headline(finished, 2, withheld=True),
        "stopped-0": headline(stopped, 0, withheld=False),
        "stopped-2": headline(stopped, 2, withheld=False),
        "killed-0": headline(RunStatus.KILLED, 0, withheld=False),
        "killed-2": headline(RunStatus.KILLED, 2, withheld=False),
    } == {
        "finished-0": "The run finished inside the cage, and the task got done.",
        "finished-1": (
            "The agent tried to cross the line. Sandcage blocked it once, and the task still "
            "got done."
        ),
        "finished-2": DEMO_HEADLINE,
        "withheld-0": (
            "The run finished inside the cage, but the patch is withheld until you review it."
        ),
        "withheld-2": (
            "The agent tried to cross the line. Sandcage blocked it 2 times, and the patch is "
            "withheld until you review it."
        ),
        "stopped-0": "You stopped this run. Nothing left the cage.",
        "stopped-2": "You stopped this run. Before that, Sandcage blocked it 2 times.",
        "killed-0": "The run ended before the task was done.",
        "killed-2": "Sandcage blocked the agent 2 times. The run ended before the task was done.",
    }


def test_declined_line_wording_snapshot() -> None:
    assert declined_line(0) is None
    assert declined_line(1) == "You did not approve 1 request, so it did not run."
    assert declined_line(2) == "You did not approve 2 requests, so they did not run."


@pytest.mark.parametrize("count", [0, 1, 5])
def test_no_headline_claims_a_crossing_without_a_denial(count: int) -> None:
    for status in RunStatus:
        for withheld in (False, True):
            line = headline(status, 0, withheld=withheld)
            assert "cross the line" not in line
            assert "blocked" not in line
    second = declined_line(count)
    assert second is None or "blocked" not in second


# --- through the harness ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return run_scenario(tmp_path_factory.mktemp("demo"))


@pytest.fixture(scope="module")
def refused_only(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """sneaky_urllib with the gate's ask refused: its only non-allow is that refused ask."""
    return run_scenario(
        tmp_path_factory.mktemp("refused"),
        "sneaky_urllib",
        human=ScriptedHuman(answers={ApprovalKind.ACTION: refuse}),
    )


def _steps(run: Run) -> list[StepEvent]:
    return [e for e in run.result.events if isinstance(e, StepEvent)]


def test_the_scripted_demo_keeps_the_exact_demo_wording(demo: Run) -> None:
    report = demo.result.report
    assert report is not None
    assert report.event.headline == DEMO_HEADLINE
    assert (report.event.blocks, report.event.declined, report.event.declined_line) == (
        2,
        0,
        None,
    )
    assert f"**Delivered.** {DEMO_HEADLINE}" in report.markdown
    outputs = demo.result.outputs
    assert outputs is not None
    assert f"**Delivered.** {DEMO_HEADLINE}" in outputs.summary_md
    assert "did not approve" not in outputs.summary_md + report.markdown


def test_a_run_whose_only_non_allows_are_refused_asks_claims_no_block(refused_only: Run) -> None:
    steps = _steps(refused_only)
    declined = [e for e in steps if is_declined(e)]
    assert declined, "the scenario must raise an ask that the human refuses"
    assert not [e for e in steps if is_denied(e)]
    report = refused_only.result.report
    assert report is not None
    event = report.event
    assert (event.blocks, event.declined) == (0, len(declined))
    outputs = refused_only.result.outputs
    assert outputs is not None
    for text in (event.headline, report.markdown, outputs.summary_md):
        assert "cross the line" not in text
        assert "Sandcage blocked" not in text
    assert event.declined_line == declined_line(len(declined))
    assert event.declined_line is not None
    assert event.declined_line in report.markdown
    assert report.data["blocks"] == []
    assert isinstance(report.data["declined"], list)
    assert len(report.data["declined"]) == len(declined)
    assert "## Blocked (0)" in report.markdown
    assert f"## Not approved ({len(declined)})" in report.markdown


def test_report_event_counts_match_its_data(demo: Run, refused_only: Run) -> None:
    for run in (demo, refused_only):
        report = run.result.report
        assert report is not None
        assert isinstance(report.data["blocks"], list)
        assert isinstance(report.data["declined"], list)
        assert report.event.blocks == len(report.data["blocks"])
        assert report.event.declined == len(report.data["declined"])
        assert report.data["declined_line"] == report.event.declined_line


def test_the_sample_stream_shows_the_demo_headline() -> None:
    sample = Path(__file__).parents[1] / "contract" / "samples" / "demo-run.jsonl"
    text = sample.read_text(encoding="utf-8")
    assert DEMO_HEADLINE in text
    assert '"declined": 0' in text
