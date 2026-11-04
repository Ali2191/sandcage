"""Checkpoints: file copies (never git), exact restores of changed, deleted and created
files, the 20 MB cap failing loudly, and symlink-safe restores."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from sandcage.agent.loop import Agent
from sandcage.agent.scenarios import FIXED_LINE, WRONG_LINE, ScenarioId, scripted_models
from sandcage.contract.events import CheckpointState, RunStatus
from sandcage.harness.checkpoints import CHECKPOINT_CAP, CheckpointError, Checkpoints
from sandcage.plan.layer import PlanLayer
from sandcage.policy.actions import Action, parse_action
from sandcage.sandbox import safefs
from sandcage.sandbox.run import SandboxRun
from tests.agent_helpers import DEMO_PLAN, IS_SECRET, LocalRunner
from tests.policy_helpers import engine
from tests.sandbox_helpers import DEMO_REPO


def _tree(tmp_path: Path) -> tuple[Path, Path, Checkpoints]:
    original = tmp_path / "original"
    (original / "pkg").mkdir(parents=True)
    (original / "pkg" / "a.py").write_text("a = 1\n")
    (original / "pkg" / "b.py").write_text("b = 1\n")
    tool = original / "run.sh"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    (original / ".git").mkdir()
    (original / ".git" / "config").write_text("[core]\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    safefs.copy_tree(
        original, workspace, leave_out=lambda _p: False, max_files=1000, max_bytes=10_000_000
    )
    checkpoints = Checkpoints(original)
    checkpoints.attach(workspace)
    return original, workspace, checkpoints


def _put(workspace: Path, path: str, text: str) -> None:
    """Write as the sandbox's file tools do (read/write for everyone)."""
    safefs.write_bytes(workspace, path, text.encode())


def _state(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path: (
            safefs.read_bytes(root, path, limit=10_000_000)[0],
            stat.S_IMODE(safefs.mode_of(root, path)),
        )
        for path in safefs.walk(root)
    }


def test_restore_reproduces_changed_deleted_and_created_files_exactly(tmp_path: Path) -> None:
    _, workspace, checkpoints = _tree(tmp_path)
    at_start = _state(workspace)
    _put(workspace, "pkg/a.py", "a = 2\n")  # changed
    (workspace / "run.sh").unlink()  # deleted (it was executable)
    _put(workspace, "pkg/new.py", "n = 1\n")  # created
    first = checkpoints.save(step=3)
    assert (first.id, first.step) == (1, 3)
    assert first.files == ("pkg/a.py", "pkg/new.py", "run.sh")
    at_one = _state(workspace)
    _put(workspace, "pkg/a.py", "a = 3\n")
    (workspace / "pkg" / "new.py").unlink()
    _put(workspace, "pkg/b.py", "b = 2\n")
    _put(workspace, "run.sh", "#!/bin/sh\nrm -rf /\n")
    _put(workspace, "pkg/later.py", "x\n")
    assert checkpoints.save().id == 2
    at_two = _state(workspace)

    changed = checkpoints.restore(1)
    assert _state(workspace) == at_one
    assert sorted(changed) == ["pkg/a.py", "pkg/b.py", "pkg/later.py", "pkg/new.py", "run.sh"]
    checkpoints.restore(0)
    assert _state(workspace) == at_start
    assert stat.S_IMODE((workspace / "run.sh").stat().st_mode) == 0o777  # 0o755 | rw for all
    checkpoints.restore(2)  # later checkpoints are kept, so a rewind can be undone
    assert _state(workspace) == at_two
    assert checkpoints.restore(2) == []


def test_an_unchanged_workspace_reuses_the_latest(tmp_path: Path) -> None:
    _, workspace, checkpoints = _tree(tmp_path)
    assert checkpoints.save().id == 0
    _put(workspace, "pkg/a.py", "a = 2\n")
    assert checkpoints.save().id == 1
    assert checkpoints.save().id == 1
    _put(workspace, "pkg/a.py", "a = 1\n")  # back to the original: a new state
    back = checkpoints.save()
    assert (back.id, back.files) == (2, ())
    assert [info.id for info in checkpoints.infos()] == list(range(checkpoints.latest + 1))


def test_contents_are_stored_once(tmp_path: Path) -> None:
    _, workspace, checkpoints = _tree(tmp_path)
    _put(workspace, "big.txt", "x" * 1000)
    checkpoints.save()
    _put(workspace, "other.txt", "y")
    checkpoints.save()
    assert checkpoints.stored_bytes == 1001


@pytest.mark.parametrize("bad", [99, -1, True, "1", None, 1.0])
def test_an_unknown_checkpoint_is_an_error_listing_the_known_ones(
    tmp_path: Path, bad: object
) -> None:
    _, _, checkpoints = _tree(tmp_path)
    with pytest.raises(CheckpointError, match="known: 0"):
        checkpoints.restore(bad)


def test_an_over_cap_checkpoint_refuses_loudly_and_stores_nothing(tmp_path: Path) -> None:
    original, workspace, _ = _tree(tmp_path)
    checkpoints = Checkpoints(original, cap=100)
    checkpoints.attach(workspace)
    _put(workspace, "small.txt", "s" * 60)
    assert checkpoints.save().id == 1
    _put(workspace, "more.txt", "m" * 50)  # 60 + 50 > 100
    with pytest.raises(CheckpointError, match="100-byte cap"):
        checkpoints.save()
    assert (checkpoints.latest, checkpoints.stored_bytes) == (1, 60)
    _put(workspace, "huge.txt", "h" * 500)  # a single file over what is left
    with pytest.raises(CheckpointError):
        checkpoints.save()
    assert checkpoints.stored_bytes == 60
    assert checkpoints.on_write("huge.txt") is None
    events = checkpoints.drain_events()
    assert [e.state for e in events] == [CheckpointState.FAILED]
    assert events[0].error is not None
    assert "cap" in events[0].error
    assert CHECKPOINT_CAP == 20 * 1024 * 1024


def test_restore_replaces_a_linked_file_and_never_writes_through_it(tmp_path: Path) -> None:
    _, workspace, checkpoints = _tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("host file\n")
    (workspace / "pkg" / "a.py").unlink()
    (workspace / "pkg" / "a.py").symlink_to(outside)
    assert checkpoints.restore(0) == ["pkg/a.py"]
    assert outside.read_text() == "host file\n"
    assert not (workspace / "pkg" / "a.py").is_symlink()
    assert (workspace / "pkg" / "a.py").read_text() == "a = 1\n"


def test_restore_refuses_a_linked_directory_and_touches_nothing_outside(tmp_path: Path) -> None:
    _, workspace, checkpoints = _tree(tmp_path)
    _put(workspace, "pkg/a.py", "a = 2\n")
    checkpoints.save()
    outside = tmp_path / "host"
    outside.mkdir()
    (outside / "a.py").write_text("host\n")
    for name in ("a.py", "b.py"):
        (workspace / "pkg" / name).unlink()
    (workspace / "pkg").rmdir()
    (workspace / "pkg").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CheckpointError, match=r"restore refused at pkg/a\.py"):
        checkpoints.restore(1)
    assert (outside / "a.py").read_text() == "host\n"
    assert sorted(p.name for p in outside.iterdir()) == ["a.py"]


def test_saving_never_follows_links(tmp_path: Path) -> None:
    _, workspace, checkpoints = _tree(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret\n")
    (workspace / "leak.txt").symlink_to(secret)
    info = checkpoints.save()
    assert "leak.txt" not in info.files
    assert b"host secret" not in b"".join(checkpoints._blobs.values())


def test_left_out_secret_files_are_not_deletions(tmp_path: Path) -> None:
    original, workspace, _ = _tree(tmp_path)
    (original / ".env").write_text("KEY=1\n")
    checkpoints = Checkpoints(original)
    checkpoints.attach(workspace, left_out=[".env"])
    assert checkpoints.save().id == 0
    _put(workspace, "pkg/a.py", "a = 2\n")
    assert ".env" not in checkpoints.save().files
    checkpoints.restore(0)
    assert not (workspace / ".env").exists()


def test_not_attached_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError, match="not attached"):
        Checkpoints(tmp_path).save()


def test_the_rollback_tool_and_human_rewind(tmp_path: Path) -> None:
    _, workspace, checkpoints = _tree(tmp_path)
    _put(workspace, "pkg/a.py", "a = 2\n")
    assert checkpoints.on_write("pkg/a.py") == 1
    _put(workspace, "pkg/a.py", "a = 3\n")
    assert checkpoints.on_write("pkg/a.py") == 2
    action = parse_action({"tool": "rollback", "args": {"checkpoint": 1}})
    assert isinstance(action, Action)
    result = checkpoints.rollback(action)
    assert result == {"checkpoint": 1, "restored": ["pkg/a.py"]}
    assert checkpoints.rewind(7, by="agent") == {
        "error": "there is no checkpoint 7; known: 0, 1, 2"
    }
    assert checkpoints.rewind(0, by="human") == {"checkpoint": 0, "restored": ["pkg/a.py"]}
    events = checkpoints.drain_events()
    assert [(e.state, e.checkpoint, e.restored_by) for e in events] == [
        (CheckpointState.SAVED, 1, None),
        (CheckpointState.SAVED, 2, None),
        (CheckpointState.RESTORED, 1, "agent"),
        (CheckpointState.RESTORED, 0, "human"),
    ]
    assert checkpoints.drain_events() == []
    assert "- 2: saved (pkg/a.py)" in checkpoints.summary()


def _demo_run(tmp_path: Path, runner_factory: object) -> tuple[SandboxRun, Checkpoints]:
    checkpoints = Checkpoints(DEMO_REPO)
    run = SandboxRun(
        DEMO_REPO,
        engine=engine(),
        runner_factory=runner_factory,  # type: ignore[arg-type]
        on_write=checkpoints.on_write,
        tools={"rollback": checkpoints.rollback},
        layers=(PlanLayer(DEMO_PLAN, engine().rulebook.context),),
        temp_root=tmp_path,
    )
    return run, checkpoints


def test_rollback_end_to_end_through_a_real_sandbox_run(tmp_path: Path) -> None:
    run, checkpoints = _demo_run(tmp_path, LocalRunner)
    models = scripted_models(ScenarioId.ROLLBACK)
    with run:
        checkpoints.attach(run.workspace.path, run.workspace.left_out)
        result = Agent(models.executor, run, "rollback0001", IS_SECRET).run_task("Fix it.")
        fixed = run.read_text("invoice_calc/invoice.py")
        final = LocalRunner(run.workspace.path).run("pytest -q")
    assert result.status is RunStatus.FINISHED
    assert [e.checkpoint for e in result.events if e.tool == "write_file"] == [1, 2]
    rollback = next(e for e in result.events if e.tool == "rollback")
    assert rollback.executed
    assert fixed is not None
    assert FIXED_LINE in fixed
    assert WRONG_LINE not in fixed
    assert "5 passed" in final.output
    assert ".env" not in str(checkpoints.infos())
