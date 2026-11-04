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
from sandcage.api.main import StartupError, build
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
