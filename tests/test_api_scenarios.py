"""Every scenario end to end through the HTTP API, the way the panel drives it: start,
stream the events, answer every approval kind the scenario raises over HTTP, then read
the deliverables. Scripted models and the real SQLite store; the demo and sneaky_urllib
also on real Docker (``make test-docker``)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sandcage.agent.scenarios import FIXED_LINE
from sandcage.sandbox.container import DockerSandbox, Limits
from sandcage.sandbox.docker_api import DockerError, EngineClient
from sandcage.sandbox.run import RunnerFactory
from tests.api_helpers import TASK
from tests.http_helpers import Served, SseEvent, answer_all, approve_choice, refuse_choice, serve

ASKED = {
    "demo": ["plan", "breaker"],
    "skip_test": ["plan"],
    "rollback": ["plan"],
    "sneaky_urllib": ["plan", "action", "redteam"],
    "clarify": ["question", "plan"],
}
Chooser = Callable[[dict[str, Any]], tuple[str, str | None]]


def _run(
    api: Served, scenario: str, choose: Chooser = approve_choice
) -> tuple[str, list[SseEvent]]:
    reply = api.client.call(
        "POST", "/runs", {"repo": "invoice-calc", "task": TASK, "fake_scenario": scenario}
    )
    assert reply.status == 202, reply.body
    run_id: str = reply.json()["id"]
    return run_id, answer_all(api.client, run_id, choose=choose)


def _check_delivered(api: Served, run_id: str, seen: list[SseEvent], scenario: str) -> None:
    asked = [e.data["approval"]["kind"] for e in seen if e.kind == "approval_needed"]
    assert asked == ASKED[scenario]
    resolved = [e.data for e in seen if e.kind == "approval_resolved"]
    assert [r["by"] for r in resolved] == ["human"] * len(asked)  # none timed out
    done = seen[-1]
    assert done.kind == "done"
    assert (done.data["status"], done.data["outcome"], done.data["delivered"]) == (
        "finished",
        "Delivered",
        True,
    )
    row = api.client.call("GET", f"/runs/{run_id}").json()
    assert (row["status"], row["pending"], row["delivery_blocked"]) == ("finished", None, False)
    assert row["released_by_human"] is (scenario == "sneaky_urllib")
    patch = api.client.call("GET", f"/runs/{run_id}/patch")
    assert patch.status == 200
    assert ("+" + FIXED_LINE.rstrip("\n")).encode() in patch.body
    report = api.client.call("GET", f"/runs/{run_id}/report").json()
    assert report["outcome"] == "Delivered"
    if scenario == "demo":
        assert report["headline"] == (
            "The agent tried to cross the line. Sandcage blocked it 2 times, and the task "
            "still got done."
        )
        sentences = [
            e.data["blocked_sentence"] for e in seen if e.kind == "step" and e.data["blocked"]
        ]
        assert sentences == [
            "Tried to send your .env to collector.example.net",
            "Tried to read your secrets in .env",
        ]


@pytest.mark.parametrize("scenario", sorted(ASKED))
def test_scenario_over_http(tmp_path: Path, scenario: str) -> None:
    with serve(tmp_path) as api:
        run_id, seen = _run(api, scenario)
        _check_delivered(api, run_id, seen, scenario)


def test_sneaky_kept_blocked_over_http_then_released(tmp_path: Path) -> None:
    def keep(approval: dict[str, Any]) -> tuple[str, str | None]:
        return (
            refuse_choice(approval) if approval["kind"] == "redteam" else approve_choice(approval)
        )

    with serve(tmp_path) as api:
        run_id, seen = _run(api, "sneaky_urllib", keep)
        assert seen[-1].data["outcome"] == "Withheld"
        assert api.client.call("GET", f"/runs/{run_id}/patch").status == 423
        assert api.client.call("POST", f"/runs/{run_id}/release", {}).status == 200
        assert api.client.call("GET", f"/runs/{run_id}/patch").status == 200


@pytest.mark.parametrize(("scenario", "kind"), [("demo", "plan"), ("demo", "breaker"),
                                                ("sneaky_urllib", "action"),
                                                ("sneaky_urllib", "redteam"),
                                                ("clarify", "question")])  # fmt: skip
def test_stop_over_http_from_every_approval_kind_delivers_nothing(
    tmp_path: Path, scenario: str, kind: str
) -> None:
    with serve(tmp_path) as api:
        reply = api.client.call(
            "POST", "/runs", {"repo": "invoice-calc", "task": TASK, "fake_scenario": scenario}
        )
        run_id = reply.json()["id"]
        seen: list[SseEvent] = []
        with api.client.stream(run_id) as events:
            for item in events:
                if not isinstance(item, SseEvent):
                    continue
                seen.append(item)
                if item.kind == "approval_needed":
                    approval = item.data["approval"]
                    if approval["kind"] == kind:
                        stopped = api.client.call("POST", f"/runs/{run_id}/stop", {})
                        assert stopped.json()["status"] == "stopped"
                        again = api.client.call("POST", f"/runs/{run_id}/stop", {})
                        assert again.json()["status"] == "stopped"
                    else:
                        choice, answer = approve_choice(approval)
                        body: dict[str, Any] = {"approval_id": approval["id"], "choice": choice}
                        if answer is not None:
                            body["answer"] = answer
                        api.client.call("POST", f"/runs/{run_id}/approve", body)
        kinds = [e.kind for e in seen]
        assert kinds.count("stopped") == 1
        after = kinds[kinds.index("stopped") :]
        assert not {"verify", "redteam", "deliver", "report", "learn"} & set(after)
        assert seen[-1].data["status"] == "stopped"
        assert api.client.call("GET", f"/runs/{run_id}/patch").status == 409


# --- real Docker ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docker() -> RunnerFactory:
    client = EngineClient.from_env()
    try:
        client.ping()
    except DockerError as problem:
        pytest.fail(f"make test-docker needs a running Docker daemon: {problem}")
    if not client.image_exists("sandcage-sandbox:latest"):
        pytest.fail("build the sandbox image first: make sandbox-image")
    return lambda ws: DockerSandbox(client, ws, runtime=None, limits=Limits(timeout=30.0))


@pytest.mark.docker
@pytest.mark.parametrize("scenario", ["demo", "sneaky_urllib"])
def test_scenario_over_http_on_real_docker(
    tmp_path: Path, docker: RunnerFactory, scenario: str
) -> None:
    with serve(tmp_path, runner_factory=docker) as api:
        run_id, seen = _run(api, scenario)
        _check_delivered(api, run_id, seen, scenario)
        verify = [e.data for e in seen if e.kind == "verify"]
        assert verify[-1]["passed"] is True
        assert verify[-1]["tests"]["passed"] is True  # pytest ran inside the container


@pytest.mark.docker
def test_stop_over_http_on_real_docker_removes_the_sandbox(
    tmp_path: Path, docker: RunnerFactory
) -> None:
    client = EngineClient.from_env()
    before = {c.get("Id") for c in client.list_containers({"sandcage": "sandbox"})}
    with serve(tmp_path, runner_factory=docker) as api:
        reply = api.client.call(
            "POST", "/runs", {"repo": "invoice-calc", "task": TASK, "fake_scenario": "demo"}
        )
        run_id = reply.json()["id"]
        with api.client.stream(run_id) as events:
            for item in events:
                if isinstance(item, SseEvent) and item.kind == "approval_needed":
                    if item.data["approval"]["kind"] == "breaker":  # a container is running
                        api.client.call("POST", f"/runs/{run_id}/stop", {})
                    else:
                        choice, _ = approve_choice(item.data["approval"])
                        api.client.call(
                            "POST",
                            f"/runs/{run_id}/approve",
                            {"approval_id": item.data["approval"]["id"], "choice": choice},
                        )
        assert api.env.manager.wait(run_id, 60)
        assert api.client.call("GET", f"/runs/{run_id}").json()["status"] == "stopped"
    after = {c.get("Id") for c in client.list_containers({"sandcage": "sandbox"})}
    assert after <= before  # the run's container is gone
    assert not list(tmp_path.glob("sandcage-*"))  # and so is its workspace
