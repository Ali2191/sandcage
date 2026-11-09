"""A scripted run on a server whose provider is a real model.

The panel's Start form offers "Scripted attack demo" on every deployment and sends
``fake_scenario``. A scripted run never calls a model, so a server configured with a real
provider must still run it, with the scripted models, and must label it as scripted
everywhere: the run row, the run detail, every streamed event, the done event and both
report formats. A provider model name on a scripted run would present a simulation as a
real model's behaviour.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from sandcage.api.runs import StartRequest
from sandcage.api.settings import DemoSettings, ProviderSettings
from sandcage.harness.pipeline import Models
from tests.api_helpers import TASK, WAIT, make_env
from tests.http_helpers import CODE, Served, answer_all, serve

#: The provider model name a scripted run must never carry.
REAL_MODEL = "provider-model-that-must-not-appear"
#: Nothing listens on port 9: if a scripted run ever reached the model client, it would fail.
REAL_PROVIDER = ProviderSettings(
    provider="vultr",
    base_url="http://127.0.0.1:9/v1",
    api_key="fake-key-never-sent",
    model=REAL_MODEL,
    retries=0,
)
#: Exactly what the panel's Start form sends for "Scripted attack demo" > "Poisoned README".
PANEL_SCRIPTED_DEMO: dict[str, JsonValue] = {
    "repo": "invoice-calc",
    "fake_scenario": "demo",
    "task": "Set up the project following the README, then fix the failing test.",
    "guard": True,
    "approve_plan": False,
}


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Served]:
    demo = DemoSettings(access_code=CODE, runs_per_client_per_hour=100, runs_per_day=100)
    with serve(tmp_path, demo=demo, provider=REAL_PROVIDER) as served:
        yield served


def test_the_start_forms_scripted_demo_runs_on_a_real_provider_server(api: Served) -> None:
    reply = api.client.call("POST", "/runs", PANEL_SCRIPTED_DEMO)
    assert reply.status == 202, reply.body
    run_id: str = reply.json()["id"]
    events = answer_all(api.client, run_id)
    done = events[-1]
    assert done.kind == "done"
    # The scripted demo reached its end: two blocked rows and a delivered patch, which no
    # model call could have produced (the provider address accepts no connection).
    assert done.data["status"] == "finished", done.data
    assert done.data["delivered"] is True


def test_a_scripted_run_never_carries_the_provider_model(api: Served) -> None:
    reply = api.client.call("POST", "/runs", PANEL_SCRIPTED_DEMO)
    assert reply.status == 202, reply.body
    run_id: str = reply.json()["id"]
    events = answer_all(api.client, run_id)
    api.env.manager.wait(run_id, WAIT)

    for event in events:
        assert REAL_MODEL not in json.dumps(event.data), event.kind
    done = events[-1].data
    assert done["provider"] == "scripted"
    assert done["model"] is None

    row = next(r for r in api.client.call("GET", "/runs").json()["runs"] if r["id"] == run_id)
    assert (row["provider"], row["model"]) == ("scripted", None)
    detail = api.client.call("GET", f"/runs/{run_id}")
    assert detail.status == 200
    report = api.client.call("GET", f"/runs/{run_id}/report")
    assert report.json()["model"] is None
    markdown = api.client.call("GET", f"/runs/{run_id}/report?format=md").body.decode()
    assert "scripted (no real model)" in markdown
    for body in (detail.body, report.body, markdown.encode()):
        assert REAL_MODEL.encode() not in body


def test_a_scripted_run_that_fails_early_is_still_labelled_scripted(tmp_path: Path) -> None:
    def broken(_request: StartRequest) -> Models:
        raise RuntimeError("no models today")

    env = make_env(tmp_path, provider=REAL_PROVIDER, models_factory=broken)
    run_id = env.manager.start(
        StartRequest(repo="invoice-calc", task=TASK, fake_scenario="demo"), client="x"
    )
    assert env.manager.wait(run_id, WAIT)
    record = env.store.get_run(run_id)
    assert record is not None
    assert (record.provider, record.model, record.status) == ("scripted", None, "error")
    done = env.store.events(run_id)[-1]
    assert REAL_MODEL not in done.model_dump_json()
