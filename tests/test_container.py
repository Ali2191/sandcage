"""The container, proven without Docker: host-owned deadline under an injected client stall,
bounded exit-code read under stale inspects, output cap, kill-all, hardened create, and
the orphan reaper."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from sandcage.sandbox import container
from sandcage.sandbox.container import (
    CommandResult,
    DockerSandbox,
    Limits,
    OutputBuffer,
    SandboxClosedError,
    SandboxImageMissingError,
    reap_orphaned_sandboxes,
)
from sandcage.sandbox.docker_api import DockerError, DockerUnavailableError
from sandcage.sandbox.hardening import OwnerIdentity, hardened_config
from sandcage.sandbox.workspace import Workspace
from tests.fake_docker import KILL_ALL, FakeDocker, Script

IDENTITY = OwnerIdentity("api-host", 1, "aaaa1111")
FAST = Limits(timeout=0.3, host_grace=1.0, exit_code_wait=0.3, exit_code_poll=0.01)


def sandbox(docker: FakeDocker, tmp_path: Path, limits: Limits = FAST) -> DockerSandbox:
    box = DockerSandbox(docker, tmp_path, runtime=None, identity=IDENTITY, limits=limits)
    box.start()
    return box


def scripted(docker: FakeDocker, script: Script) -> None:
    docker.scripts = lambda command: (
        script if "sh" in command[0] and len(command) == 5 else Script()
    )


# --- the two timeout fault-injection tests (Bugs #1) ---------------------------------


def test_a_client_stall_after_the_command_starts_still_times_out(tmp_path: Path) -> None:
    """exec_start returns late (the command is already running). A stopwatch started
    after it would see too little time pass; the host deadline was set before it."""
    docker = FakeDocker()
    docker.exec_start_stall = 0.2
    scripted(docker, Script(output=[b"working\n"], hold_open=True))
    box = sandbox(docker, tmp_path)
    started = time.monotonic()
    result = box.run("python slow_tests.py")
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert result.output == "working\n"
    assert result.exit_code == 137
    assert elapsed < 0.3 + 0.2 + 1.0  # deadline plus the stall, never the backstop
    assert any(KILL_ALL in " ".join(command) for command in docker.commands())


def test_the_verdict_comes_from_the_host_deadline_not_a_late_stopwatch(tmp_path: Path) -> None:
    """The command needs 0.8 s and exec_start returns 0.2 s after it started. The host
    deadline (0.3 s, set before the start) fires while the output is still open, so the
    verdict is "timed out" and the command is killed, whatever the exit code says."""
    docker = FakeDocker()
    docker.exec_start_stall = 0.2
    scripted(docker, Script(output=[b"slow\n"], runs_for=0.8, exit_code=0))
    result = sandbox(docker, tmp_path).run("python slow.py")
    assert (result.timed_out, result.exit_code) == (True, 137)


def test_a_command_that_ends_within_the_deadline_is_not_timed_out_despite_a_stall(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    docker.exec_start_stall = 0.2
    scripted(docker, Script(output=[b"quick\n"], runs_for=0.05, exit_code=0))
    result = sandbox(docker, tmp_path, Limits(timeout=1.0, exit_code_poll=0.01)).run("true")
    assert (result.timed_out, result.exit_code, result.output) == (False, 0, "quick\n")


def test_a_stale_exit_code_read_waits_for_docker_to_record_it(tmp_path: Path) -> None:
    """Right after the stream closes, exec_inspect can still say running with no code."""
    docker = FakeDocker()
    scripted(docker, Script(output=[b"3 passed\n"], exit_code=1, stale_inspects=5))
    result = sandbox(docker, tmp_path).run("pytest -q")
    assert result == CommandResult(
        exit_code=1, output="3 passed\n", truncated=False, timed_out=False
    )


def test_an_exit_code_docker_never_records_is_none_after_a_bounded_wait(tmp_path: Path) -> None:
    docker = FakeDocker()
    scripted(docker, Script(output=[b"x"], exit_code=None))
    started = time.monotonic()
    result = sandbox(docker, tmp_path).run("true")
    assert (result.exit_code, result.timed_out) == (None, False)
    assert time.monotonic() - started < FAST.exit_code_wait + 0.5


def test_exit_124_by_itself_is_not_a_timeout(tmp_path: Path) -> None:
    docker = FakeDocker()
    scripted(docker, Script(output=[b"done\n"], exit_code=124))
    result = sandbox(docker, tmp_path, Limits(timeout=5.0)).run("exit 124")
    assert (result.exit_code, result.timed_out) == (124, False)


# --- output, kill-all, wrapper -------------------------------------------------------


def test_output_is_capped_but_fully_drained(tmp_path: Path) -> None:
    docker = FakeDocker()
    chunks = [b"a" * 10_000 for _ in range(5)]
    scripted(docker, Script(output=chunks))
    box = sandbox(docker, tmp_path, Limits(timeout=5.0))
    result = box.run("yes | head -c 50000")
    assert result.truncated
    assert len(result.output) == 20_000
    main = next(r for r in docker.execs.values() if len(r.command) == 5)
    assert main.finished  # every chunk was read, so the command could not block


def test_output_buffer_counts_past_its_limit() -> None:
    buffer = OutputBuffer(5)
    buffer.append(b"abc")
    buffer.append(b"defgh")
    assert (buffer.text(), buffer.total, buffer.truncated) == ("abcde", 8, True)
    exact = OutputBuffer(3)
    exact.append(b"abc")
    assert not exact.truncated


def test_every_command_is_followed_by_a_kill_all(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(docker, tmp_path, Limits(timeout=5.0))
    box.run("echo one")
    box.run("echo two")
    commands = [" ".join(c) for c in docker.commands()]
    assert len(commands) == 4
    assert KILL_ALL in commands[1]
    assert KILL_ALL in commands[3]


def test_commands_run_as_the_sandbox_user_inside_the_backstop_wrapper(tmp_path: Path) -> None:
    docker = FakeDocker()
    sandbox(docker, tmp_path, Limits(timeout=30.0, host_grace=5.0)).run("pytest -q 'a b'")
    first = next(iter(docker.execs.values()))
    assert first.user == "65534:65534"
    assert first.command == [
        "sh",
        "-c",
        'umask 000 && exec timeout -s KILL "$1" sh -c "$0"',
        "pytest -q 'a b'",
        "35",
    ]
    assert all(record.user == "65534:65534" for record in docker.execs.values())


def test_a_docker_failure_during_a_command_propagates(tmp_path: Path) -> None:
    docker = FakeDocker()

    box = sandbox(docker, tmp_path, Limits(timeout=5.0))

    docker.failures["exec_inspect"] = DockerUnavailableError(
        "cannot reach Docker (ConnectionResetError)"
    )
    with pytest.raises(DockerUnavailableError):
        box.run("echo hi")


# --- the hardened create -------------------------------------------------------------


def test_the_container_is_created_hardened(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(docker, tmp_path)
    (config,) = docker.created
    assert config == hardened_config(
        command=["sleep", "infinity"], identity=IDENTITY, workspace=tmp_path, runtime=None
    )
    host = config["HostConfig"]
    assert isinstance(host, dict)
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert (host["PidsLimit"], host["Memory"], host["MemorySwap"], host["NanoCpus"]) == (
        128,
        512 * 1024 * 1024,
        512 * 1024 * 1024,
        1_000_000_000,
    )
    assert host["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=64m"}
    assert host["Binds"] == [f"{tmp_path}:/workspace:rw"]
    assert "Runtime" not in host
    assert "Privileged" not in host
    assert config["User"] == "65534:65534"
    assert config["Env"] == []
    assert config["NetworkDisabled"] is True
    assert config["Labels"] == {
        "sandcage": "sandbox",
        "sandcage.owner": "api-host:1",
        "sandcage.instance": "aaaa1111",
    }
    assert box.container_id is not None


def test_gvisor_runtime_is_requested_when_selected(tmp_path: Path) -> None:
    docker = FakeDocker()
    DockerSandbox(docker, tmp_path, runtime="runsc", identity=IDENTITY).start()
    host = docker.created[0]["HostConfig"]
    assert isinstance(host, dict)
    assert host["Runtime"] == "runsc"


def test_a_missing_image_is_a_clear_error_and_creates_nothing(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.images.clear()
    with pytest.raises(SandboxImageMissingError, match="make sandbox-image"):
        DockerSandbox(docker, tmp_path, runtime=None, identity=IDENTITY).start()
    assert docker.created == []


def test_a_failed_start_removes_the_container(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.failures["start_container"] = DockerError("Docker answered 500: no")
    box = DockerSandbox(docker, tmp_path, runtime=None, identity=IDENTITY)
    with pytest.raises(DockerError):
        box.start()
    assert docker.removed == ["c0001"]
    assert docker.containers == {}


def test_close_is_idempotent_and_tolerates_a_missing_container(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(docker, tmp_path)
    container_id = box.container_id
    assert container_id is not None
    docker.containers.pop(container_id)  # Docker already removed it
    box.close()
    box.close()
    with pytest.raises(SandboxClosedError):
        box.run("echo late")


def test_close_opens_permissions_before_removing(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(docker, tmp_path)
    box.close()
    assert any("chmod -R a+rwX /workspace" in " ".join(c) for c in docker.commands())
    assert docker.removed == ["c0001"]


# --- orphan reaping (Bugs #8) --------------------------------------------------------


def listed(
    container_id: str, owner: str, instance: str, source: str | None = None
) -> dict[str, object]:
    mounts = [] if source is None else [{"Destination": "/workspace", "Source": source}]
    return {
        "Id": container_id,
        "Labels": {"sandcage": "sandbox", "sandcage.owner": owner, "sandcage.instance": instance},
        "Mounts": mounts,
    }


def test_reaper_removes_only_orphans_of_this_host(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    dead_ws = Workspace.create(repo, temp_root=temp_root)
    restarted_ws = Workspace.create(repo, temp_root=temp_root)
    live_ws = Workspace.create(repo, temp_root=temp_root)

    docker = FakeDocker()
    docker.listing = [
        listed("dead", "api-host:77", "x", str(dead_ws.path)),
        listed("restarted", "api-host:1", "old-instance", str(restarted_ws.path)),
        listed("mine", "api-host:1", "aaaa1111", str(live_ws.path)),
        listed("live", "api-host:88", "y"),
        listed("other-host", "elsewhere:77", "z"),
        {"Id": "unlabelled", "Labels": {"sandcage": "sandbox"}},
        listed("garbled", "api-host:notapid", "z"),
    ]
    reaped = reap_orphaned_sandboxes(
        docker, identity=IDENTITY, temp_root=temp_root, alive=lambda pid: pid == 88
    )
    assert reaped.containers == ("dead", "restarted")
    assert set(reaped.workspaces) == {str(dead_ws.run_dir), str(restarted_ws.run_dir)}
    assert reaped.problems == ()
    assert not dead_ws.exists
    assert not restarted_ws.exists
    assert live_ws.exists
    assert {item["Id"] for item in docker.listing} == {
        "mine",
        "live",
        "other-host",
        "unlabelled",
        "garbled",
    }
    live_ws.cleanup()


def test_reaper_never_deletes_a_path_that_is_not_our_workspace(tmp_path: Path) -> None:
    precious = tmp_path / "home" / "workspace"
    precious.mkdir(parents=True)
    (precious / "keep.txt").write_text("keep")
    docker = FakeDocker()
    docker.listing = [listed("dead", "api-host:77", "x", str(precious))]
    reaped = reap_orphaned_sandboxes(
        docker, identity=IDENTITY, temp_root=tmp_path / "tmp", alive=lambda _: False
    )
    assert reaped.containers == ("dead",)
    assert reaped.workspaces == ()
    assert (precious / "keep.txt").read_text() == "keep"


def test_reaper_reports_a_removal_failure_and_keeps_going(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.listing = [listed("a", "api-host:77", "x"), listed("b", "api-host:78", "x")]
    real_remove = docker.remove_container

    def flaky(container_id: str) -> None:
        if container_id == "a":
            raise DockerError("Docker answered 500: busy")
        real_remove(container_id)

    docker.remove_container = flaky  # type: ignore[method-assign]
    reaped = reap_orphaned_sandboxes(docker, identity=IDENTITY, alive=lambda _: False)
    assert reaped.containers == ("b",)
    assert reaped.problems == ("a: Docker answered 500: busy",)


def test_pid_alive() -> None:
    import os

    assert container.pid_alive(os.getpid())
    assert not container.pid_alive(0)
    assert not container.pid_alive(-5)
