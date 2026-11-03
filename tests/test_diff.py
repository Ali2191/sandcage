"""The diff builder: git-applicable masked patches, hidden secret files, honest counts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sandcage.sandbox import diff
from sandcage.sandbox.diff import ChangeKind, FileChange, workspace_diff
from sandcage.sandbox.workspace import Workspace
from tests.sandbox_helpers import DEMO_REPO, DEMO_SECRET_VALUE, is_secret
from tests.secrets_fixtures import STRIPE_KEY


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    original, workspace = tmp_path / "original", tmp_path / "workspace"
    for root in (original, workspace):
        (root / "pkg").mkdir(parents=True)
        (root / "pkg/app.py").write_text("a = 1\nb = 2\n")
        (root / "README.md").write_text("# demo\n")
        (root / "old.txt").write_text("bye\n")
    return original, workspace


def build(original: Path, workspace: Path, left_out: tuple[str, ...] = ()) -> diff.WorkspaceDiff:
    return workspace_diff(original, workspace, is_secret=is_secret(), left_out=left_out)


def git_apply(original: Path, patch: str, tmp_path: Path) -> Path:
    """Apply the patch to a scratch copy of ``original`` with git, and return the copy."""
    target = tmp_path / "applied"
    shutil.copytree(original, target, symlinks=True)
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(patch)
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - fixed git binary on our own scratch directory
        [git, "apply", "--whitespace=nowarn", str(patch_file)],
        cwd=target,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target


def test_no_change_is_an_empty_diff(pair: tuple[Path, Path]) -> None:
    result = build(*pair)
    assert result.empty
    assert (result.patch, result.secrets, result.truncated) == ("", {}, False)


def test_added_modified_and_deleted_files(pair: tuple[Path, Path], tmp_path: Path) -> None:
    original, workspace = pair
    (workspace / "pkg/app.py").write_text("a = 1\nb = 3\nc = 4\n")
    (workspace / "pkg/new.py").write_text("print('new')\n")
    (workspace / "old.txt").unlink()

    result = build(original, workspace)

    assert result.files == (
        FileChange("old.txt", ChangeKind.DELETED, added_lines=0, removed_lines=1),
        FileChange("pkg/app.py", ChangeKind.MODIFIED, added_lines=2, removed_lines=1),
        FileChange("pkg/new.py", ChangeKind.ADDED, added_lines=1, removed_lines=0),
    )
    assert "diff --git a/pkg/new.py b/pkg/new.py\nnew file mode 100644\n--- /dev/null\n" in (
        result.patch
    )
    assert "deleted file mode 100644\n--- a/old.txt\n+++ /dev/null\n" in result.patch
    applied = git_apply(original, result.patch, tmp_path)
    assert (applied / "pkg/app.py").read_text() == "a = 1\nb = 3\nc = 4\n"
    assert (applied / "pkg/new.py").read_text() == "print('new')\n"
    assert not (applied / "old.txt").exists()


def test_missing_final_newline_still_applies(pair: tuple[Path, Path], tmp_path: Path) -> None:
    original, workspace = pair
    (workspace / "pkg/app.py").write_text("a = 1\nb = 2")
    result = build(original, workspace)
    assert "\\ No newline at end of file\n" in result.patch
    assert (git_apply(original, result.patch, tmp_path) / "pkg/app.py").read_text() == (
        "a = 1\nb = 2"
    )


def test_a_changed_secret_file_is_hidden(pair: tuple[Path, Path], tmp_path: Path) -> None:
    original, workspace = pair
    (original / ".env").write_text("TOKEN=old-value\n")
    (workspace / ".env").write_text("TOKEN=new-value\n")
    (workspace / "README.md").write_text("# demo, edited\n")
    result = build(original, workspace)
    assert result.files == (
        FileChange(".env", ChangeKind.MODIFIED, secret=True),
        FileChange("README.md", ChangeKind.MODIFIED, added_lines=1, removed_lines=1),
    )
    assert "old-value" not in result.patch
    assert "new-value" not in result.patch
    assert "contents hidden" in result.patch
    assert "diff --git a/.env" not in result.patch
    assert result.secrets == {".env": ("secret_file",)}
    applied = git_apply(original, result.patch, tmp_path)
    assert (applied / ".env").read_text() == "TOKEN=old-value\n"
    assert (applied / "README.md").read_text() == "# demo, edited\n"


def test_a_secret_left_out_of_the_copy_is_not_a_deletion(tmp_path: Path) -> None:
    with Workspace.create(DEMO_REPO, leave_out=is_secret(), temp_root=tmp_path) as ws:
        result = build(DEMO_REPO, ws.path, ws.left_out)
        assert result.empty


def test_a_secret_file_the_agent_creates_in_a_left_out_place_is_hidden(tmp_path: Path) -> None:
    with Workspace.create(DEMO_REPO, leave_out=is_secret(), temp_root=tmp_path) as ws:
        (ws.path / ".env").write_text("STOLEN=1\n")
        result = build(DEMO_REPO, ws.path, ws.left_out)
        assert result.files == (FileChange(".env", ChangeKind.ADDED, secret=True),)
        assert "STOLEN" not in result.patch


def test_secrets_added_to_code_are_found_and_masked(pair: tuple[Path, Path]) -> None:
    original, workspace = pair
    (workspace / "pkg/app.py").write_text(
        f'a = 1\nb = 2\nSTRIPE = "{STRIPE_KEY}"\nowner = "dev@example.com"\n'
    )
    result = build(original, workspace)
    assert STRIPE_KEY not in result.patch
    assert "[MASKED:" in result.patch
    assert "dev@example.com" not in result.patch
    assert result.secrets == {"pkg/app.py": ("stripe_key",)}


def test_secrets_only_in_removed_or_kept_lines_do_not_count(pair: tuple[Path, Path]) -> None:
    original, workspace = pair
    (original / "pkg/app.py").write_text(f'KEY = "{STRIPE_KEY}"\nx = 1\n')
    (workspace / "pkg/app.py").write_text(f'KEY = "{STRIPE_KEY}"\nx = 2\n')
    result = build(original, workspace)
    assert result.secrets == {}
    assert STRIPE_KEY not in result.patch


def test_binary_files_are_noted_not_shown(pair: tuple[Path, Path]) -> None:
    original, workspace = pair
    (workspace / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    result = build(original, workspace)
    assert result.files == (FileChange("logo.png", ChangeKind.ADDED, binary=True),)
    assert result.patch == "# logo.png: binary or very large file added, not shown\n"


def test_large_files_compare_by_digest(
    pair: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, workspace = pair
    monkeypatch.setattr(diff, "FILE_LIMIT", 10)
    (original / "big.txt").write_text("x" * 50)
    (workspace / "big.txt").write_text("x" * 50)
    assert build(original, workspace).empty
    (workspace / "big.txt").write_text("x" * 49 + "y")
    result = build(original, workspace)
    assert result.files == (FileChange("big.txt", ChangeKind.MODIFIED, binary=True),)


def test_links_and_noise_directories_are_never_diffed(pair: tuple[Path, Path]) -> None:
    original, workspace = pair
    (workspace / "escape").symlink_to("/etc/passwd")
    (workspace / ".git").mkdir()
    (workspace / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (workspace / "pkg/__pycache__").mkdir()
    (workspace / "pkg/__pycache__/app.pyc").write_bytes(b"\x00")
    assert build(original, workspace).empty


def test_the_patch_is_capped(pair: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    original, workspace = pair
    monkeypatch.setattr(diff, "PATCH_LIMIT", 50)
    (workspace / "pkg/app.py").write_text("line\n" * 100)
    result = build(original, workspace)
    assert result.truncated
    assert result.patch.endswith(diff.TRUNCATION_MARKER)
    assert len(result.patch) == 50 + len(diff.TRUNCATION_MARKER)
    assert result.files[0].added_lines == 100


def test_the_demo_fix_produces_a_clean_patch(tmp_path: Path) -> None:
    with Workspace.create(DEMO_REPO, leave_out=is_secret(), temp_root=tmp_path) as ws:
        source = ws.read_text("invoice_calc/invoice.py")
        assert source is not None
        ws.write_file(
            "invoice_calc/invoice.py",
            source.replace("return amount - percent", "return amount * (100 - percent) / 100"),
        )
        result = build(DEMO_REPO, ws.path, ws.left_out)
    assert [(f.path, f.kind) for f in result.files] == [
        ("invoice_calc/invoice.py", ChangeKind.MODIFIED)
    ]
    assert "-    return amount - percent\n+    return amount * (100 - percent) / 100\n" in (
        result.patch
    )
    assert DEMO_SECRET_VALUE not in result.patch
