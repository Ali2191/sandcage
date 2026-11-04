"""Startup: public mode fails closed, one API process per event log, cut-off runs become
interrupted before serving, Docker being unreachable is not fatal, and the runtime is
reported truthfully in readiness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sandcage.api.app import Request, Response
from sandcage.api.main import Built, StartupError, build
from sandcage.events.store import EventStore
from tests.agent_helpers import LocalRunner
from tests.fake_docker import FakeDocker

CODE = "a-long-enough-code"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "SANDCAGE_DATA_DIR": str(tmp_path / "data"),
        "TMPDIR": str(tmp_path),
        "SANDBOX_RUNTIME": "runc",
        **extra,
    }


def test_public_mode_refuses_to_start_without_a_12_char_code(tmp_path: Path) -> None:
    for code in ("", "short-code1"):
        env = _env(tmp_path, DEMO_MODE="public", DEMO_ACCESS_CODE=code)
        with pytest.raises(StartupError, match="12 characters") as caught:
            build(env, api=FakeDocker(), runner_factory=LocalRunner)
        if code:
            assert code not in str(caught.value)
    assert not (tmp_path / "data" / "sandcage.db").exists()  # refused before the store


def test_the_command_line_refuses_public_mode_without_a_code(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "sandcage.api"],
        env={**_env(tmp_path, DEMO_MODE="public"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2
    assert "not starting" in done.stderr
    assert "DEMO_ACCESS_CODE" in done.stderr


def test_one_api_process_per_event_log(tmp_path: Path) -> None:
    first = build(_env(tmp_path), api=FakeDocker(), runner_factory=LocalRunner)
    with pytest.raises(StartupError, match="another API process"):
        build(_env(tmp_path), api=FakeDocker(), runner_factory=LocalRunner)
    first.lock.close()
    first.store.close()
    again = build(_env(tmp_path), api=FakeDocker(), runner_factory=LocalRunner)
    again.lock.close()


def test_startup_recovers_cut_off_runs_and_survives_docker_being_down(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    store = EventStore(tmp_path / "data" / "sandcage.db")
    store.create_run("aaaaaaaaaaaa", repo="invoice-calc", task="t", guard=True)
    store.close()
    docker = FakeDocker()
    docker.unavailable = True
    built = build(
        _env(tmp_path, DEMO_ACCESS_CODE=CODE, SANDBOX_RUNTIME="runsc"),
        api=docker,
        runner_factory=LocalRunner,
    )
    assert built.store.get_run("aaaaaaaaaaaa").status.value == "interrupted"
    ready = built.app.handle(Request("GET", "/readyz"))
    assert isinstance(ready, Response)
    assert ready.status == 503
    checks = json.loads(ready.body)["checks"]
    assert checks["docker"]["ok"] is False
    assert checks["runtime"]["ok"] is False  # gVisor asked for, Docker down: unknown
    assert checks["runtime"]["active"] is None
    assert CODE not in ready.body.decode()
    built.lock.close()


def test_readiness_reports_the_runtime_truthfully(tmp_path: Path) -> None:
    built = build(
        _env(tmp_path, SANDBOX_RUNTIME="runsc"), api=FakeDocker(), runner_factory=LocalRunner
    )
    ready = built.app.handle(Request("GET", "/readyz"))
    assert isinstance(ready, Response)
    runtime = json.loads(ready.body)["checks"]["runtime"]
    assert (runtime["active"], runtime["gvisor"]) == ("runc", False)
    assert "not registered" in runtime["detail"]
    status = built.app.handle(Request("GET", "/demo/status"))
    assert isinstance(status, Response)
    assert json.loads(status.body)["sandbox_runtime"]["label"] == "runc (plain Docker)"
    built.lock.close()


def test_a_real_provider_without_a_key_serves_but_refuses_runs(tmp_path: Path) -> None:
    built = build(
        _env(tmp_path, LLM_PROVIDER="vultr", LLM_MODEL="m"),
        api=FakeDocker(),
        runner_factory=LocalRunner,
    )
    ready = built.app.handle(Request("GET", "/readyz"))
    assert isinstance(ready, Response)
    assert json.loads(ready.body)["checks"]["provider"]["ok"] is False
    started = built.app.handle(
        Request("POST", "/runs", body=b'{"repo": "invoice-calc", "task": "fix it"}')
    )
    assert isinstance(started, Response)
    assert started.status == 503
    built.lock.close()


def _checks(built: Built) -> dict[str, dict[str, object]]:
    ready = built.app.handle(Request("GET", "/readyz"))
    assert isinstance(ready, Response)
    body = ready.body.decode()
    assert CODE not in body
    found: dict[str, dict[str, object]] = json.loads(body)["checks"]
    return found


def test_readiness_names_every_check_the_deploy_relies_on(tmp_path: Path) -> None:
    built = build(
        _env(tmp_path, DEMO_MODE="public", DEMO_ACCESS_CODE=CODE),
        api=FakeDocker(),
        runner_factory=LocalRunner,
    )
    checks = _checks(built)
    assert set(checks) == {"store", "docker", "image", "runtime", "disk", "provider", "demo"}
    assert all(check["ok"] for check in checks.values())
    assert checks["demo"]["detail"] == "public mode, access code configured"
    built.lock.close()


def test_a_missing_sandbox_image_makes_the_demo_not_ready(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.images.clear()
    built = build(_env(tmp_path), api=docker, runner_factory=LocalRunner)
    ready = built.app.handle(Request("GET", "/readyz"))
    assert isinstance(ready, Response)
    assert ready.status == 503
    image = _checks(built)["image"]
    assert image["ok"] is False
    assert "build it on the host" in str(image["detail"])
    built.lock.close()


def test_an_unwritable_event_log_makes_the_demo_not_ready(tmp_path: Path) -> None:
    built = build(_env(tmp_path), api=FakeDocker(), runner_factory=LocalRunner)
    built.store.close()  # the log cannot take a write any more
    ready = built.app.handle(Request("GET", "/readyz"))
    assert isinstance(ready, Response)
    assert ready.status == 503
    assert _checks(built)["store"]["ok"] is False
    built.lock.close()


def test_public_mode_without_a_long_code_is_never_reported_ready(tmp_path: Path) -> None:
    built = build(
        _env(tmp_path, DEMO_MODE="public", DEMO_ACCESS_CODE=CODE),
        api=FakeDocker(),
        runner_factory=LocalRunner,
    )
    # The settings object refuses this state at startup; /readyz must not depend on that alone.
    object.__setattr__(built.app.settings.demo, "access_code", "short")
    ready = built.app.handle(Request("GET", "/readyz"))
    assert isinstance(ready, Response)
    assert ready.status == 503
    demo = _checks(built)["demo"]
    assert demo["ok"] is False
    assert "short" not in str(demo["detail"])
    built.lock.close()


def test_startup_removes_scenario_repos_a_killed_process_left_but_not_fresh_ones(
    tmp_path: Path,
) -> None:
    import os
    import time

    stale = tmp_path / "sandcage-bench-abcd1234"
    fresh = tmp_path / "sandcage-bench-wxyz9876"
    for path in (stale, fresh):
        path.mkdir()
        (path / ".env").write_text("fake", encoding="utf-8")
    long_ago = time.time() - 3600
    os.utime(stale, (long_ago, long_ago))
    built = build(_env(tmp_path), api=FakeDocker(), runner_factory=LocalRunner)
    assert not stale.exists()
    assert fresh.exists()
    built.lock.close()
