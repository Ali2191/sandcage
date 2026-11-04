"""The HTTP API through a real server on 127.0.0.1: every changing route needs the code,
the code never reaches a log, oversized bodies are refused unread, run ids cannot reach
files or SQL, CORS for one origin, the SSE stream (replay, resume, heartbeat, end) and the
documented shape of every endpoint."""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from sandcage.api.settings import BODY_LIMIT, DemoSettings
from tests.api_helpers import TASK, WAIT
from tests.http_helpers import (
    CODE,
    Served,
    SseEvent,
    answer_all,
    refuse_choice,
    serve,
)

PANEL = "http://127.0.0.1:3000"
RUN = "0123456789ab"

# Every route that changes state. A new one must be added here, so it is tested below.
CHANGING = {
    ("POST", "/demo/unlock"),
    ("POST", "/runs"),
    ("POST", "/runs/{id}/stop"),
    ("POST", "/runs/{id}/approve"),
    ("POST", "/runs/{id}/rewind"),
    ("POST", "/runs/{id}/release"),
    ("POST", "/runs/{id}/learn/accept"),
}


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Served]:
    with serve(tmp_path, panel_origin=PANEL) as served:
        yield served


def _start(api: Served, scenario: str = "demo", **extra: Any) -> str:
    body = {"repo": "invoice-calc", "task": TASK, "fake_scenario": scenario, **extra}
    reply = api.client.call("POST", "/runs", body)
    assert reply.status == 202, reply.body
    run_id: str = reply.json()["id"]
    return run_id


def _template(route: str) -> str:
    return route.replace("(?P<run_id>[0-9a-f]{12})", "{id}")


# --- the access code ---------------------------------------------------------------------


def test_the_changing_routes_are_exactly_the_post_routes(api: Served) -> None:
    changing = {(r.method, _template(r.pattern.pattern)) for r in api.app.routes if r.changes}
    posts = {(r.method, _template(r.pattern.pattern)) for r in api.app.routes if r.method != "GET"}
    assert changing == posts == CHANGING


@pytest.mark.parametrize(("method", "route"), sorted(CHANGING))
def test_every_changing_route_requires_the_code(api: Served, method: str, route: str) -> None:
    path = route.replace("{id}", RUN)
    missing = api.client.call(method, path, {}, code=None)
    assert missing.status == 401, (path, missing.body)
    wrong = api.client.call(method, path, {}, code="not-the-code")
    assert wrong.status == 403
    assert CODE not in (missing.body + wrong.body).decode()
    right = api.client.call(method, path, {})
    assert right.status not in {401, 403}


def test_reading_stays_open_without_a_code(api: Served) -> None:
    for path in ("/healthz", "/demo/status", "/repos", "/policy", "/runs", "/bench/scenarios"):
        assert api.client.call("GET", path, code=None).status == 200, path


def test_unlock_checks_the_code_and_wrong_codes_are_rate_limited(api: Served) -> None:
    assert api.client.call("POST", "/demo/unlock").json() == {"ok": True}
    for _ in range(10):
        assert api.client.call("POST", "/demo/unlock", code="guess").status == 403
    locked = api.client.call("POST", "/demo/unlock")
    assert locked.status == 429
    assert int(locked.headers["retry-after"]) > 0


def test_the_kill_switch_answers_before_the_code(tmp_path: Path) -> None:
    demo = DemoSettings(access_code=CODE, paused=True)
    with serve(tmp_path, demo=demo) as api:
        reply = api.client.call("POST", "/runs", {"repo": "invoice-calc", "task": TASK}, code=None)
        assert reply.status == 503
        assert "paused" in reply.json()["detail"]
        assert api.client.call("GET", "/runs", code=None).status == 200  # reading open
        assert api.client.call("GET", "/demo/status").json()["paused"] is True


def test_the_code_never_appears_in_logs(
    api: Served, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    run_id = _start(api)
    answer_all(api.client, run_id)
    api.client.call("POST", "/demo/unlock", code="wrong-" + CODE)
    api.client.call("GET", f"/runs/{run_id}?code={CODE[:3]}")
    out, err = capfd.readouterr()
    for text in (caplog.text, out, err):
        assert CODE not in text
    assert any("POST /runs" in record.getMessage() for record in caplog.records)


# --- request limits --------------------------------------------------------------------------


def _raw(api: Served, request: bytes, *, send_body: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", api.port), timeout=10) as sock:
        sock.sendall(request + send_body)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


def test_an_oversized_body_is_refused_unread(api: Served) -> None:
    head = (
        f"POST /runs HTTP/1.1\r\nHost: x\r\nX-Access-Code: {CODE}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {BODY_LIMIT + 1}\r\n\r\n"
    ).encode()
    reply = _raw(api, head)  # no body is sent at all: the answer cannot have waited for it
    assert reply.startswith(b"HTTP/1.1 413")
    assert api.env.store.list_runs()[1] == 0


def test_a_chunked_body_is_refused(api: Served) -> None:
    head = (
        f"POST /runs HTTP/1.1\r\nHost: x\r\nX-Access-Code: {CODE}\r\n"
        "Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    ).encode()
    assert _raw(api, head).startswith(b"HTTP/1.1 411")


def test_a_false_length_never_reads_past_it(api: Served) -> None:
    body = json.dumps({"repo": "invoice-calc", "task": "x" * 100_000}).encode()
    head = (
        f"POST /runs HTTP/1.1\r\nHost: x\r\nX-Access-Code: {CODE}\r\nContent-Length: 2\r\n\r\n"
    ).encode()
    reply = _raw(api, head, send_body=body)
    assert reply.startswith(b"HTTP/1.1 400")  # only '{"' was read: not JSON
    assert api.env.store.list_runs()[1] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/runs/../../etc/passwd",
        "/runs/%2e%2e%2fsandcage.db",
        "/runs/0123456789ab'%20OR%201=1--",
        "/runs/0123456789AB",
        "/runs/0123456789abc",
        "/runs/0123456789a",
        "/runs/0123456789ab/../../patch",
        "/runs/;DROP%20TABLE%20runs/patch",
    ],
)
def test_run_ids_cannot_reach_files_or_sql(api: Served, path: str) -> None:
    assert api.client.call("GET", path).status == 404
    assert api.client.call("POST", path + "/stop", {}).status == 404
    assert api.env.store.list_runs()[1] == 0  # the table is still there and empty


def test_an_unknown_run_is_404_everywhere(api: Served) -> None:
    for method, route in [
        ("GET", "/runs/{id}"),
        ("GET", "/runs/{id}/events"),
        ("GET", "/runs/{id}/patch"),
        ("GET", "/runs/{id}/report"),
        ("GET", "/runs/{id}/checkpoints"),
        ("GET", "/runs/{id}/learn"),
        ("POST", "/runs/{id}/stop"),
        ("POST", "/runs/{id}/release"),
    ]:
        reply = api.client.call(
            method, route.replace("{id}", RUN), {} if method == "POST" else None
        )
        assert reply.status == 404, route
        assert set(reply.json()) == {"detail"}


def test_bad_bodies_are_422(api: Served) -> None:
    bodies: list[JsonValue] = [
        {"repo": "invoice-calc"},
        {"task": TASK},
        {"repo": "invoice-calc", "scenario": "x", "task": TASK},
        {"repo": "invoice-calc", "task": "x" * 2001},
        {"repo": "invoice-calc", "task": TASK, "guard": "yes"},
        {"repo": "invoice-calc", "task": TASK, "path": "/etc"},
        {"repo": "invoice-calc", "task": TASK, "done_criteria": ["a"] * 9},
        {"repo": "invoice-calc", "task": TASK, "fake_scenario": "nope"},
    ]
    for body in bodies:
        assert api.client.call("POST", "/runs", body).status == 422, body
    assert api.client.call("POST", "/runs", b"{not json").status == 400
    unknown: JsonValue = {"repo": "/etc", "task": TASK}
    assert api.client.call("POST", "/runs", unknown).status == 404
    assert api.client.call("POST", "/runs", {"scenario": "x", "task": TASK}).status == 404
    assert api.client.call("GET", "/runs?limit=0").status == 422
    assert api.client.call("GET", "/runs?limit=101").status == 422


# --- CORS ------------------------------------------------------------------------------------


def test_cors_only_for_the_panel_origin(api: Served) -> None:
    api.client.origin = PANEL
    preflight = api.client.call("OPTIONS", "/runs", code=None)
    assert preflight.status == 204
    assert preflight.headers["access-control-allow-origin"] == PANEL
    assert preflight.headers["access-control-allow-methods"] == "GET, POST"
    allowed = preflight.headers["access-control-allow-headers"]
    assert {h.strip() for h in allowed.split(",")} == {
        "Content-Type",
        "Last-Event-ID",
        "X-Access-Code",
    }
    reply = api.client.call("GET", "/demo/status")
    assert reply.headers["access-control-allow-origin"] == PANEL
    assert reply.headers["access-control-expose-headers"] == "Retry-After"
    api.client.origin = "https://evil.example"
    other = api.client.call("OPTIONS", "/runs", code=None)
    assert "access-control-allow-origin" not in other.headers
    assert "access-control-allow-origin" not in api.client.call("GET", "/repos").headers


def test_no_cors_without_a_panel_origin(tmp_path: Path) -> None:
    with serve(tmp_path) as api:
        api.client.origin = PANEL
        assert "access-control-allow-origin" not in api.client.call("OPTIONS", "/runs").headers
        assert "access-control-allow-origin" not in api.client.call("GET", "/repos").headers


# --- health and catalogue ------------------------------------------------------------------


def test_health_and_readiness(tmp_path: Path) -> None:
    def failing() -> dict[str, dict[str, Any]]:
        return {
            "docker": {"ok": False, "detail": "Docker is unreachable"},
            "runtime": {"ok": True, "detail": "gVisor (runsc) is not registered", "active": "runc"},
        }

    with serve(tmp_path, readiness=failing) as api:
        assert api.client.call("GET", "/healthz").json() == {"ok": True}
        ready = api.client.call("GET", "/readyz")
        assert ready.status == 503
        body = ready.json()
        assert body["ready"] is False
        assert body["checks"]["runtime"]["active"] == "runc"
        assert body["checks"]["provider"]["ok"] is True
        assert CODE not in ready.body.decode()
    with serve(tmp_path / "ok") as api:
        assert api.client.call("GET", "/readyz").status == 200


def test_catalogue_shapes(api: Served, tmp_path: Path) -> None:
    assert api.client.call("GET", "/repos").json() == {"repos": ["invoice-calc"]}
    policy = api.client.call("GET", "/policy").json()
    assert set(policy) == {"rules", "secret_patterns", "counts"}
    assert set(policy["rules"][0]) == {"id", "decision", "risk", "reason", "tools"}
    assert policy["counts"] == {"deny": 12, "ask": 4, "allow": 6}
    assert ".env" in policy["secret_patterns"]
    gallery = api.client.call("GET", "/bench/scenarios").json()
    assert gallery["total"] == 12  # the Bench catalogue (S8); the full shape is in test_api_bench
    assert api.client.call("GET", "/bench/results").status == 404
    (tmp_path / "results.json").write_text(json.dumps({"provider": "fake", "scenarios": []}))
    assert api.client.call("GET", "/bench/results").json()["provider"] == "fake"
    status = api.client.call("GET", "/demo/status").json()
    for key in ("access_required", "paused", "message", "runs_left_today", "runs_per_day",
                "active_runs", "max_concurrent", "sandbox_runtime"):  # fmt: skip
        assert key in status
    assert status["sandbox_runtime"]["label"] == "runc (plain Docker)"


# --- runs over HTTP --------------------------------------------------------------------------


def test_a_demo_run_over_http_with_every_deliverable(api: Served) -> None:
    run_id = _start(api)
    seen = answer_all(api.client, run_id)
    assert seen[-1].kind == "done"
    assert [e.id for e in seen] == sorted({e.id for e in seen})
    row = api.client.call("GET", f"/runs/{run_id}").json()
    for key in ("id", "repo", "task", "guard", "status", "created", "finished", "provider",
                "model", "steps", "counts", "tokens", "tokens_reported", "released_by_human",
                "delivery_blocked", "pending", "result", "server_time", "outcome"):  # fmt: skip
        assert key in row, key
    assert (row["repo"], row["task"], row["status"], row["pending"]) == (
        "invoice-calc",
        TASK,
        "finished",
        None,
    )
    assert row["counts"]["denied"] == 2
    assert row["result"]["kind"] == "done"
    listing = api.client.call("GET", "/runs").json()
    assert (listing["total"], listing["limit"], listing["offset"]) == (1, 20, 0)
    assert listing["runs"][0]["id"] == run_id
    patch = api.client.call("GET", f"/runs/{run_id}/patch")
    assert patch.status == 200
    assert 'filename="sandcage.patch"' in patch.headers["content-disposition"]
    assert b"+    return amount - amount * percent / Decimal(100)" in patch.body
    assert api.client.call("GET", f"/runs/{run_id}/summary").body.startswith(b"# Sandcage summary")
    report = api.client.call("GET", f"/runs/{run_id}/report").json()
    assert report["headline"].startswith("The agent tried to cross the line.")
    md = api.client.call("GET", f"/runs/{run_id}/report?format=md")
    assert md.body.startswith(b"# Sandcage run report")
    assert api.client.call("GET", f"/runs/{run_id}/report?format=pdf").status == 422
    learn = api.client.call("GET", f"/runs/{run_id}/learn").json()
    assert set(learn) == {"suggestions", "notes"}
    accepted = api.client.call(
        "POST", f"/runs/{run_id}/learn/accept", {"suggestion": learn["suggestions"][0]["id"]}
    )
    assert accepted.status == 200
    assert accepted.json()["file"] == "learned.yaml"
    again = api.client.call(
        "POST", f"/runs/{run_id}/learn/accept", {"suggestion": learn["suggestions"][0]["id"]}
    )
    assert again.status == 409
    checkpoints = api.client.call("GET", f"/runs/{run_id}/checkpoints").json()["checkpoints"]
    assert [c["id"] for c in checkpoints] == [0, 1]
    assert api.client.call("POST", f"/runs/{run_id}/rewind", {"checkpoint": 0}).status == 409
    assert api.client.call("POST", f"/runs/{run_id}/stop", {}).json() == {
        "id": run_id,
        "status": "finished",
    }


def test_a_withheld_patch_is_423_until_released(api: Served) -> None:
    run_id = _start(api, "sneaky_urllib")

    def keep_blocked(approval: dict[str, Any]) -> tuple[str, str | None]:
        if approval["kind"] == "redteam":
            return refuse_choice(approval)
        return next(c["id"] for c in approval["choices"] if c["approves"]), None

    answer_all(api.client, run_id, choose=keep_blocked)
    row = api.client.call("GET", f"/runs/{run_id}").json()
    assert (row["delivery_blocked"], row["released_by_human"], row["outcome"]) == (
        True,
        False,
        "Withheld",
    )
    withheld = api.client.call("GET", f"/runs/{run_id}/patch")
    assert withheld.status == 423
    assert "/release" not in withheld.json()["detail"]  # plain words, no developer path
    assert api.client.call("GET", f"/runs/{run_id}/summary").status == 200
    assert api.client.call("POST", f"/runs/{run_id}/release", {}).status == 200
    row = api.client.call("GET", f"/runs/{run_id}").json()
    assert (row["delivery_blocked"], row["released_by_human"]) == (False, True)
    assert api.client.call("GET", f"/runs/{run_id}/patch").status == 200
    report = api.client.call("GET", f"/runs/{run_id}/report").json()
    assert report["released_by_human"] is True
    assert api.client.call("POST", f"/runs/{run_id}/release", {}).status == 409


def test_approve_with_nothing_pending_is_409_and_a_bad_choice_422(api: Served) -> None:
    run_id = _start(api)
    with api.client.stream(run_id) as events:
        for item in events:
            if isinstance(item, SseEvent) and item.kind == "approval_needed":
                approval = item.data["approval"]
                bad = api.client.call(
                    "POST",
                    f"/runs/{run_id}/approve",
                    {"approval_id": approval["id"], "choice": "x"},
                )
                assert bad.status == 422
                stale = api.client.call(
                    "POST", f"/runs/{run_id}/approve", {"approval_id": "other", "choice": "allow"}
                )
                assert stale.status == 409
                stop = api.client.call("POST", f"/runs/{run_id}/stop", {})
                assert stop.json()["status"] == "stopped"
            if isinstance(item, SseEvent) and item.kind == "done":
                break
    assert api.client.call("GET", f"/runs/{run_id}").json()["status"] == "stopped"


def test_busy_is_409_naming_the_running_ids(tmp_path: Path) -> None:
    demo = DemoSettings(access_code=CODE, max_concurrent=1, runs_per_client_per_hour=100)
    with serve(tmp_path, demo=demo) as api:
        first = _start(api)
        busy = api.client.call("POST", "/runs", {"repo": "invoice-calc", "task": TASK})
        assert busy.status == 409
        assert first in busy.json()["detail"]
        api.client.call("POST", f"/runs/{first}/stop", {})


def test_limits_answer_429_with_retry_after(tmp_path: Path) -> None:
    demo = DemoSettings(access_code=CODE, runs_per_client_per_hour=1)
    with serve(tmp_path, demo=demo) as api:
        run_id = _start(api)
        api.client.call("POST", f"/runs/{run_id}/stop", {})
        limited = api.client.call("POST", "/runs", {"repo": "invoice-calc", "task": TASK})
        assert limited.status == 429
        assert int(limited.headers["retry-after"]) > 0


# --- the event stream ------------------------------------------------------------------------


def test_sse_replays_from_an_id_and_resumes_after_a_dropped_connection(api: Served) -> None:
    run_id = _start(api)
    full = answer_all(api.client, run_id)
    ids = [e.id for e in full]
    middle = ids[len(ids) // 2]
    with api.client.stream(run_id, last_id=middle) as events:
        replayed = [e for e in events if isinstance(e, SseEvent)]
    assert [e.id for e in replayed] == [i for i in ids if i > middle]
    assert replayed[-1].kind == "done"
    first_part: list[SseEvent] = []
    with api.client.stream(run_id) as events:  # drop the connection after five events
        for item in events:
            if isinstance(item, SseEvent):
                first_part.append(item)
            if len(first_part) == 5:
                break
    with api.client.stream(run_id, last_id=first_part[-1].id) as events:
        rest = [e for e in events if isinstance(e, SseEvent)]
    assert [e.id for e in first_part + rest] == ids  # nothing lost, nothing twice
    began = time.monotonic()
    with api.client.stream(run_id, last_id=ids[-1]) as events:  # resumed after the end
        assert list(events) == []
    assert time.monotonic() - began < api.app.heartbeat / 2  # ends at once, no idle wait
    assert (
        api.client.call("GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "x"}).status
        == 422
    )


def test_sse_sends_heartbeats_while_waiting_and_resumes_mid_run(tmp_path: Path) -> None:
    with serve(tmp_path, heartbeat=0.05) as api:
        run_id = _start(api)
        pings = 0
        last = 0
        with api.client.stream(run_id) as events:
            for item in events:
                if isinstance(item, SseEvent):
                    last = item.id
                elif item == ": ping":
                    pings += 1
                if pings >= 2:
                    break  # the plan approval is waiting: the stream is idle but alive
        assert last > 0
        # a reloaded panel reads the waiting question from the run, then resumes the stream
        pending = api.client.call("GET", f"/runs/{run_id}").json()["pending"]
        assert pending["kind"] == "plan"
        plan_yes = next(c["id"] for c in pending["choices"] if c["approves"])
        reply = api.client.call(
            "POST", f"/runs/{run_id}/approve", {"approval_id": pending["id"], "choice": plan_yes}
        )
        assert reply.status == 200
        seen = []
        with api.client.stream(run_id, last_id=last) as events:
            for item in events:
                if isinstance(item, SseEvent):
                    seen.append(item)
                    if item.kind == "approval_needed":
                        approval = item.data["approval"]
                        choice = next(c["id"] for c in approval["choices"] if c["approves"])
                        api.client.call(
                            "POST",
                            f"/runs/{run_id}/approve",
                            {"approval_id": approval["id"], "choice": choice},
                        )
        assert seen[-1].kind == "done"
        assert seen[-1].data["status"] == "finished"  # answered, not timed out
        assert all(e.id > last for e in seen)


def test_a_restart_ends_the_stream_of_a_cut_off_run(tmp_path: Path) -> None:
    from sandcage.events.store import EventStore
    from tests.api_helpers import make_env

    store = EventStore(tmp_path / "cut.db")
    store.create_run(RUN, repo="invoice-calc", task="t", guard=True)
    make_env(tmp_path / "env", store=store).manager.recover()
    events = store.wait_for_events(RUN, after_id=0, timeout=WAIT)
    assert [e.kind for e in events] == ["done"]
    assert store.get_run(RUN).status.value == "interrupted"
