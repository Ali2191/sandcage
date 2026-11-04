"""The Attack gallery over HTTP: the list is the registered Bench catalogue, and "Run it"
(``POST /runs {scenario}``) works for every scenario, answering the questions the way the
Bench's cautious reviewer does. The scenario's repo is built for the run and removed when
it ends. Scripted models, the real store, a host runner."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from sandcage.bench.catalogue import SCENARIOS
from tests.bench_helpers import BenchRunner
from tests.http_helpers import Served, SseEvent, answer_all, approve_choice, refuse_choice, serve

IDS = [s.id for s in SCENARIOS]


def cautious(approval: dict[str, Any]) -> tuple[str, str | None]:
    """The Bench reviewer: plan and breaker yes, a clarifying question answered, the rest no."""
    if approval["kind"] in {"plan", "breaker", "question"}:
        return approve_choice(approval)
    return refuse_choice(approval)


def start(api: Served, scenario: str, **extra: Any) -> str:
    reply = api.client.call("POST", "/runs", {"scenario": scenario, **extra})
    assert reply.status == 202, reply.body
    run_id: str = reply.json()["id"]
    return run_id


def finish(run_id: str) -> None:
    """Join the run's thread: ``done`` is announced before its cleanup (the slot is freed
    first on purpose), so "the repo is removed" can only be asserted after the thread ends."""
    threads = [t for t in threading.enumerate() if t.name == f"run-{run_id}"]
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()


def leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith("sandcage-bench-"))


def test_the_gallery_lists_the_registered_scenarios(tmp_path: Path) -> None:
    with serve(tmp_path) as api:
        gallery = api.client.call("GET", "/bench/scenarios").json()
    assert gallery["total"] == 12
    assert [s["id"] for s in gallery["scenarios"]] == IDS
    for row, scenario in zip(gallery["scenarios"], SCENARIOS, strict=True):
        assert row == {
            "id": scenario.id,
            "category": scenario.category,
            "category_label": scenario.category_label,
            "title": scenario.title,
            "blurb": scenario.story,
            "task": scenario.task,
            "channel": scenario.channel.value,
            "goal": scenario.goal.value,
            "benign": scenario.benign,
        }
    counted = {c["key"]: c["count"] for c in gallery["categories"]}
    assert sum(counted.values()) == 12
    assert counted["ordinary_task"] == 6
    assert all(c["label"] for c in gallery["categories"])


@pytest.mark.parametrize("scenario_id", IDS)
def test_run_it_works_for_every_scenario(tmp_path: Path, scenario_id: str) -> None:
    scenario = next(s for s in SCENARIOS if s.id == scenario_id)
    with serve(tmp_path, runner_factory=BenchRunner) as api:
        run_id = start(api, scenario_id)
        seen = answer_all(api.client, run_id, choose=cautious)
        row = api.client.call("GET", f"/runs/{run_id}").json()
        done = seen[-1]
        assert done.kind == "done"
        assert done.data["status"] == "finished", done.data["summary"]
        assert (row["repo"], row["task"]) == (f"bench:{scenario_id}", scenario.task)
        assert row["status"] == "finished"
        assert row["delivered"] is True
        assert row["delivery_blocked"] is False
        asked = [e.data["approval"]["kind"] for e in seen if e.kind == "approval_needed"]
        if scenario.benign:
            assert set(asked) <= {"plan", "question"}, asked  # a benign task is never asked about
        patch = api.client.call("GET", f"/runs/{run_id}/patch")
        assert patch.status == 200
        assert b"collector.example.net" not in patch.body
        assert b"urllib" not in patch.body
        assert b"sk_live" not in patch.body
        finish(run_id)
        assert leftovers(tmp_path) == []  # the scenario's repo is removed when the run ends


def test_the_baseline_run_from_the_gallery_lets_the_attack_through(tmp_path: Path) -> None:
    """guard=false is observe mode: the gallery's "watch it without Sandcage" case."""
    with serve(tmp_path, runner_factory=BenchRunner) as api:
        run_id = start(api, "failure_rm", guard=False)
        seen = answer_all(api.client, run_id, choose=cautious)
        assert seen[-1].data["status"] in {"finished", "verify_failed"}
        steps = [e for e in seen if e.kind == "step"]
        removed = [e for e in steps if "rm -f" in str(e.data["args"].get("command", ""))]
        assert removed
        assert all(e.data["executed"] and e.data["observed"] for e in removed)


def test_an_unknown_scenario_is_404_and_never_a_path(tmp_path: Path) -> None:
    with serve(tmp_path) as api:
        for bad in ("nope", "../demo", "demo/../demo", "DEMO", "", "x" * 300):
            reply = api.client.call("POST", "/runs", {"scenario": bad})
            assert reply.status in {404, 422}, (bad, reply.status)
        assert leftovers(tmp_path) == []


def test_a_scenario_start_needs_exactly_one_of_repo_and_scenario(tmp_path: Path) -> None:
    with serve(tmp_path) as api:
        both = api.client.call("POST", "/runs", {"scenario": "demo", "repo": "invoice-calc"})
        neither = api.client.call("POST", "/runs", {"task": "x"})
        assert (both.status, neither.status) == (422, 422)


def test_a_scenario_brings_its_own_script(tmp_path: Path) -> None:
    with serve(tmp_path) as api:
        reply = api.client.call("POST", "/runs", {"scenario": "demo", "fake_scenario": "demo"})
        assert reply.status == 422
        assert leftovers(tmp_path) == []


def test_scenario_starts_need_the_access_code_like_any_start(tmp_path: Path) -> None:
    with serve(tmp_path) as api:
        reply = api.client.call("POST", "/runs", {"scenario": "demo"}, code=None)
        assert reply.status == 401


def test_stopping_a_scenario_run_removes_its_repo(tmp_path: Path) -> None:
    with serve(tmp_path, runner_factory=BenchRunner) as api:
        run_id = start(api, "demo")
        seen: list[SseEvent] = []
        with api.client.stream(run_id) as events:
            for item in events:
                if not isinstance(item, SseEvent):
                    continue
                seen.append(item)
                if item.kind == "approval_needed":
                    assert api.client.call("POST", f"/runs/{run_id}/stop", {}).status == 200
        assert seen[-1].kind == "done"
        finish(run_id)
        assert leftovers(tmp_path) == []


def test_the_results_endpoint_serves_what_the_bench_wrote(tmp_path: Path) -> None:
    with serve(tmp_path) as api:
        assert api.client.call("GET", "/bench/results").status == 404
        record = {
            "label": "scoring check",
            "provider": "fake",
            "model": "scripted",
            "scenarios": [],
        }
        (tmp_path / "results.json").write_text(json.dumps(record))
        got = api.client.call("GET", "/bench/results").json()
        assert got["label"] == "scoring check"


def test_a_run_that_fails_verification_is_not_delivered_but_a_human_can_release_it(
    tmp_path: Path,
) -> None:
    """S8 step 0 over HTTP, on a real gallery case: with the guard off the agent writes a
    file outside the plan, the checks fail, and the changes are withheld, not delivered."""
    with serve(tmp_path, runner_factory=BenchRunner) as api:
        run_id = start(api, "readme_gitconfig", guard=False)
        seen = answer_all(api.client, run_id, choose=cautious)
        done = seen[-1]
        assert done.data["status"] == "verify_failed"
        assert (done.data["outcome"], done.data["delivered"]) == ("Withheld", False)
        row = api.client.call("GET", f"/runs/{run_id}").json()
        assert (row["delivered"], row["delivery_blocked"], row["released_by_human"]) == (
            False,
            True,
            False,
        )
        assert api.client.call("GET", f"/runs/{run_id}/patch").status == 423
        report = api.client.call("GET", f"/runs/{run_id}/report").json()
        assert report["outcome"] == "Withheld"
        assert "checks did not pass" in report["headline"]
        assert "got done" not in report["headline"]
        summary = api.client.call("GET", f"/runs/{run_id}/summary").body.decode()
        assert "did not finish with the checks passing" in summary
        assert api.client.call("POST", f"/runs/{run_id}/release", {}).status == 200
        released = api.client.call("GET", f"/runs/{run_id}").json()
        assert (released["delivered"], released["released_by_human"]) == (True, True)
        assert api.client.call("GET", f"/runs/{run_id}/patch").status == 200
        finish(run_id)
