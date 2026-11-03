"""The workspace: a copy that never follows links, never writes the original, leaves secret
files out when asked, and cleans up after every failure ordering."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from sandcage.sandbox import safefs, workspace
from sandcage.sandbox.workspace import Workspace, WorkspaceError, run_temp_dir
from tests.sandbox_helpers import DEMO_REPO, DEMO_SECRET_VALUE, is_secret


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "tmp"
    root.mkdir()
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    source = tmp_path / "repo"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg/app.py").write_text("print('hi')\n")
    (source / "README.md").write_text("# demo\n")
    return source


def leftovers(temp_root: Path) -> list[str]:
    return sorted(p.name for p in temp_root.iterdir())


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


# --- the copy ------------------------------------------------------------------------


def test_copy_lives_in_a_private_temp_dir(repo: Path, temp_root: Path) -> None:
    with Workspace.create(repo, temp_root=temp_root) as ws:
        assert ws.path.parent.parent == temp_root.resolve()
        assert ws.path.parent.name.startswith("sandcage-")
        assert ws.path.name == "workspace"
        assert stat.S_IMODE(ws.run_dir.stat().st_mode) == 0o711
        assert stat.S_IMODE(ws.path.stat().st_mode) == 0o777
        assert (ws.path / "pkg/app.py").read_text() == "print('hi')\n"
        assert ws.left_out == ()
    assert leftovers(temp_root) == []


def test_symlinks_are_copied_as_links_and_never_followed(
    repo: Path, temp_root: Path, tmp_path: Path
) -> None:
    host_file = tmp_path / "host_secret.txt"
    host_file.write_text("host only")
    (repo / "escape").symlink_to(host_file)
    with Workspace.create(repo, temp_root=temp_root) as ws:
        assert (ws.path / "escape").is_symlink()
        with pytest.raises(WorkspaceError, match="symbolic link"):
            ws.read_file("escape")
        with pytest.raises(WorkspaceError, match="symbolic link"):
            ws.write_file("escape", "overwrite")
    assert host_file.read_text() == "host only"


def test_the_original_is_never_written(repo: Path, temp_root: Path) -> None:
    before = snapshot(repo)
    with Workspace.create(repo, temp_root=temp_root) as ws:
        ws.write_file("pkg/app.py", "changed\n")
        ws.write_file("new/file.py", "x = 1\n")
        assert (ws.path / "pkg/app.py").read_text() == "changed\n"
    assert snapshot(repo) == before


@pytest.mark.parametrize("bad", [None, "file"])
def test_a_missing_or_non_directory_repo_is_refused(
    tmp_path: Path, temp_root: Path, bad: str | None
) -> None:
    target = tmp_path / "not-a-repo"
    if bad == "file":
        target.write_text("x")
    with pytest.raises(WorkspaceError):
        Workspace.create(target, temp_root=temp_root)
    assert leftovers(temp_root) == []


def test_a_symlinked_repo_is_refused(repo: Path, temp_root: Path, tmp_path: Path) -> None:
    link = tmp_path / "repo-link"
    link.symlink_to(repo)
    with pytest.raises(WorkspaceError, match="not a directory"):
        Workspace.create(link, temp_root=temp_root)
    assert leftovers(temp_root) == []


# --- secret files --------------------------------------------------------------------


def test_secret_files_are_left_out_when_asked(repo: Path, temp_root: Path) -> None:
    (repo / ".env").write_text("TOKEN=abc\n")
    (repo / ".envrc").write_text("export X=1\n")
    (repo / "keys").mkdir()
    (repo / "keys/server.pem").write_text("pem")
    (repo / ".ssh").mkdir()
    (repo / ".ssh/id_rsa").write_text("key")
    (repo / "config").mkdir()
    (repo / "config/secrets.yaml").write_text("a: b\n")
    (repo / "env_link").symlink_to(".env")
    (repo / "secretary.py").write_text("ok = True\n")  # not a secret name

    with Workspace.create(repo, leave_out=is_secret(), temp_root=temp_root) as ws:
        present = {str(p.relative_to(ws.path)) for p in ws.path.rglob("*")}
        assert ws.left_out == (
            ".env",
            ".envrc",
            ".ssh",
            "config/secrets.yaml",
            "keys/server.pem",
        )
        for secret in ws.left_out:
            assert secret not in present
        assert "secretary.py" in present
        assert "pkg/app.py" in present
        assert "env_link" in present  # a dangling link: nothing to read through it
        assert ws.read_text("env_link") is None


def test_demo_repo_copy_has_no_env_file_when_enforcing(temp_root: Path) -> None:
    with Workspace.create(DEMO_REPO, leave_out=is_secret(), temp_root=temp_root) as ws:
        assert ws.left_out == (".env",)
        assert not (ws.path / ".env").exists()
        assert (ws.path / "README.md").is_file()
        assert (ws.path / "invoice_calc/invoice.py").is_file()
        for path in safefs.walk(ws.path, include_links=True):
            assert DEMO_SECRET_VALUE not in (ws.read_text(path) or "")
    assert (DEMO_REPO / ".env").read_text().count(DEMO_SECRET_VALUE) == 1


def test_observe_only_copy_keeps_the_fake_secret(temp_root: Path) -> None:
    with Workspace.create(DEMO_REPO, temp_root=temp_root) as ws:
        assert ws.left_out == ()
        assert DEMO_SECRET_VALUE in (ws.read_text(".env") or "")


# --- agent paths ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_path", "relative"),
    [
        ("pkg/app.py", "pkg/app.py"),
        ("./pkg//app.py", "pkg/app.py"),
        ("/workspace/pkg/app.py", "pkg/app.py"),
        ("/workspace", "."),
        (".", "."),
        ("pkg/../README.md", "README.md"),
    ],
)
def test_agent_paths_map_into_the_workspace(
    repo: Path, temp_root: Path, agent_path: str, relative: str
) -> None:
    with Workspace.create(repo, temp_root=temp_root) as ws:
        assert ws.relative(agent_path) == relative


@pytest.mark.parametrize(
    "agent_path", ["/etc/passwd", "../outside", "/workspace/../etc", "~/x", "$HOME/x", ""]
)
def test_paths_outside_the_workspace_are_refused(
    repo: Path, temp_root: Path, agent_path: str
) -> None:
    with Workspace.create(repo, temp_root=temp_root) as ws:
        with pytest.raises(WorkspaceError):
            ws.relative(agent_path)
        with pytest.raises(WorkspaceError):
            ws.read_file(agent_path)


# --- file tools ----------------------------------------------------------------------


def test_read_write_and_list(repo: Path, temp_root: Path) -> None:
    with Workspace.create(repo, temp_root=temp_root) as ws:
        assert ws.read_file("README.md") == workspace.FileRead("# demo\n", False)
        assert ws.read_file("README.md", limit=3) == workspace.FileRead("# d", True)
        assert ws.write_file("/workspace/out/new.txt", "héllo") == len("héllo".encode())
        assert ws.read_file("out/new.txt").content == "héllo"
        names = [(e.name, e.kind.value) for e in ws.list_dir()]
        assert names == [("README.md", "file"), ("out", "dir"), ("pkg", "dir")]
        with pytest.raises(WorkspaceError, match="no such file"):
            ws.read_file("missing.py")
        with pytest.raises(WorkspaceError, match="does not name a file"):
            ws.write_file(".", "x")


def test_writes_are_capped(repo: Path, temp_root: Path) -> None:
    with (
        Workspace.create(repo, temp_root=temp_root) as ws,
        pytest.raises(WorkspaceError, match="larger than"),
    ):
        ws.write_file("big.txt", "x" * (workspace.WRITE_LIMIT + 1))


def test_errors_never_name_host_paths(repo: Path, temp_root: Path) -> None:
    with Workspace.create(repo, temp_root=temp_root) as ws:
        for action in (
            lambda: ws.read_file("missing.py"),
            lambda: ws.read_file("/etc/passwd"),
            lambda: ws.list_dir("nothing"),
        ):
            with pytest.raises(WorkspaceError) as info:
                action()
            assert str(temp_root) not in str(info.value)
            assert str(ws.path) not in str(info.value)


def test_read_text_is_the_policy_view(repo: Path, temp_root: Path) -> None:
    with Workspace.create(repo, temp_root=temp_root) as ws:
        assert ws.read_text("pkg/app.py") == "print('hi')\n"
        assert ws.read_text("missing.py") is None
        assert ws.read_text("pkg") is None
        assert ws.read_text(".") is None
        assert ws.read_text("../etc/passwd") is None
        os.mkfifo(ws.path / "pipe")
        assert ws.read_text("pipe") is None


# --- cleanup in every failure ordering -----------------------------------------------


class BoomError(Exception):
    pass


def test_failure_while_copying_leaves_nothing(
    repo: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_midway(*args: object, **kwargs: object) -> safefs.CopyReport:
        target = args[1]
        assert isinstance(target, Path)
        (target / "partial.txt").write_text("half")
        raise safefs.LimitExceededError("big.bin", "the repository is larger than the limit")

    monkeypatch.setattr(safefs, "copy_tree", fail_midway)
    with pytest.raises(WorkspaceError, match="larger than the limit"):
        Workspace.create(repo, temp_root=temp_root)
    assert leftovers(temp_root) == []


@pytest.mark.parametrize("stage", ["chmod", "mkdir"])
def test_failure_preparing_the_dir_leaves_nothing(
    repo: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, stage, fail)
    with pytest.raises(WorkspaceError, match="Permission denied"):
        Workspace.create(repo, temp_root=temp_root)
    monkeypatch.undo()
    assert leftovers(temp_root) == []


def test_unexpected_exception_still_cleans_up(
    repo: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> safefs.CopyReport:
        raise BoomError

    monkeypatch.setattr(safefs, "copy_tree", fail)
    with pytest.raises(BoomError):
        Workspace.create(repo, temp_root=temp_root)
    assert leftovers(temp_root) == []


def test_the_real_copy_limit_cleans_up(repo: Path, temp_root: Path) -> None:
    with pytest.raises(WorkspaceError, match="more than 1 files"):
        Workspace.create(repo, temp_root=temp_root, max_files=1)
    assert leftovers(temp_root) == []


def test_cleanup_is_idempotent_and_exists_reports_it(repo: Path, temp_root: Path) -> None:
    ws = Workspace.create(repo, temp_root=temp_root)
    before = ws.exists
    ws.cleanup()
    after = ws.exists
    assert (before, after) == (True, False)
    ws.cleanup()
    assert leftovers(temp_root) == []


def test_cleanup_after_an_error_inside_the_block(repo: Path, temp_root: Path) -> None:
    with pytest.raises(BoomError), Workspace.create(repo, temp_root=temp_root):
        raise BoomError
    assert leftovers(temp_root) == []


def test_cleanup_removes_files_without_following_planted_links(
    repo: Path, temp_root: Path, tmp_path: Path
) -> None:
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "precious.txt").write_text("keep me")
    ws = Workspace.create(repo, temp_root=temp_root)
    (ws.path / "trap").symlink_to(keep)
    ws.cleanup()
    assert (keep / "precious.txt").read_text() == "keep me"


# --- the exact-path rule for deletion ------------------------------------------------


def test_run_temp_dir_accepts_only_a_workspace_we_created(repo: Path, temp_root: Path) -> None:
    with Workspace.create(repo, temp_root=temp_root) as ws:
        assert run_temp_dir(str(ws.path), temp_root) == ws.run_dir
        refused = [
            str(ws.run_dir),  # the run dir itself, not its workspace
            str(ws.path / "pkg"),  # deeper
            str(ws.path) + "/",  # not normalised
            str(ws.run_dir) + "/../" + ws.run_dir.name + "/workspace",
            "relative/sandcage-abc/workspace",
            str(temp_root.parent / "sandcage-x" / "workspace"),  # other temp root
        ]
        for path in refused:
            assert run_temp_dir(path, temp_root) is None, path


def test_run_temp_dir_refuses_other_names_links_and_owners(
    temp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = temp_root.resolve()
    other = real / "other-abc" / "workspace"
    other.mkdir(parents=True)
    assert run_temp_dir(str(other), temp_root) is None

    target = tmp_path / "elsewhere" / "workspace"
    target.mkdir(parents=True)
    (real / "sandcage-link").symlink_to(target.parent)
    assert run_temp_dir(str(real / "sandcage-link" / "workspace"), temp_root) is None

    mine = real / "sandcage-mine" / "workspace"
    mine.mkdir(parents=True)
    assert run_temp_dir(str(mine), temp_root) == mine.parent
    someone_else = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: someone_else)
    assert run_temp_dir(str(mine), temp_root) is None
