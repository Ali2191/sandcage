"""safefs: no host-side file operation ever follows a link, hangs on a FIFO or escapes."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from sandcage.sandbox import safefs
from sandcage.sandbox.safefs import (
    EntryKind,
    FileRefusedError,
    LimitExceededError,
    LinkInPathError,
    MissingPathError,
    NotRegularFileError,
    UnsafePathError,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A host directory the workspace must never reach."""
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.txt").write_text("host secret\n")
    return target


def finishes(action: Callable[[], object], seconds: float = 5.0) -> bool:
    """Run ``action`` in a thread; True if it returned (or raised) within ``seconds``."""
    done = threading.Event()

    def target() -> None:
        try:
            action()
        except (OSError, ValueError):
            pass
        finally:
            done.set()

    threading.Thread(target=target, daemon=True).start()
    return done.wait(seconds)


# --- path text -----------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", ".", "./", "/etc/passwd", "a/../b", "..", "a/..", "x\x00y"])
def test_parts_of_refuses_unsafe_paths(bad: str) -> None:
    with pytest.raises(UnsafePathError):
        safefs.parts_of(bad)


def test_parts_of_drops_dots_and_empty_parts() -> None:
    assert safefs.parts_of("./a//b/./c.txt") == ("a", "b", "c.txt")
    assert safefs.parts_of(".", allow_root=True) == ()


# --- reading -------------------------------------------------------------------------


def test_read_returns_content_and_truncation(root: Path) -> None:
    (root / "a.txt").write_bytes(b"0123456789")
    assert safefs.read_bytes(root, "a.txt", limit=100) == (b"0123456789", False)
    assert safefs.read_bytes(root, "a.txt", limit=4) == (b"0123", True)
    assert safefs.read_bytes(root, "a.txt", limit=10) == (b"0123456789", False)


def test_read_missing_and_directory(root: Path) -> None:
    (root / "d").mkdir()
    with pytest.raises(MissingPathError):
        safefs.read_bytes(root, "nope.txt", limit=10)
    with pytest.raises(MissingPathError):
        safefs.read_bytes(root, "nodir/x.txt", limit=10)
    with pytest.raises(NotRegularFileError):
        safefs.read_bytes(root, "d", limit=10)


def test_a_file_used_as_a_directory_is_refused(root: Path) -> None:
    (root / "f").write_text("x")
    with pytest.raises(NotRegularFileError, match="not a directory"):
        safefs.read_bytes(root, "f/x", limit=10)


def test_final_symlink_is_never_followed_even_inside(root: Path, outside: Path) -> None:
    (root / "real.txt").write_text("inside")
    (root / "inner_link").symlink_to("real.txt")
    (root / "outer_link").symlink_to(outside / "secret.txt")
    for name in ("inner_link", "outer_link"):
        with pytest.raises(LinkInPathError):
            safefs.read_bytes(root, name, limit=100)
        assert safefs.try_read(root, name, limit=100) is None


def test_symlinked_parent_is_never_followed(root: Path, outside: Path) -> None:
    (root / "sub").symlink_to(outside)
    with pytest.raises(LinkInPathError):
        safefs.read_bytes(root, "sub/secret.txt", limit=100)
    with pytest.raises(LinkInPathError):
        safefs.write_bytes(root, "sub/planted.txt", b"x")
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


def test_reading_a_fifo_cannot_hang_the_host(root: Path) -> None:
    os.mkfifo(root / "pipe")
    assert finishes(lambda: safefs.read_bytes(root, "pipe", limit=10))
    with pytest.raises(NotRegularFileError):
        safefs.read_bytes(root, "pipe", limit=10)


def test_writing_a_fifo_cannot_hang_the_host(root: Path) -> None:
    os.mkfifo(root / "pipe")
    assert finishes(lambda: safefs.write_bytes(root, "pipe", b"x"))
    with pytest.raises(NotRegularFileError):
        safefs.write_bytes(root, "pipe", b"x")


def test_writing_a_fifo_with_a_reader_attached_writes_nothing(root: Path) -> None:
    os.mkfifo(root / "pipe")
    reader = os.open(root / "pipe", os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(NotRegularFileError):
            safefs.write_bytes(root, "pipe", b"payload")
        assert os.read(reader, 100) == b""
    finally:
        os.close(reader)


def test_try_read_returns_none_for_every_problem(root: Path) -> None:
    (root / "d").mkdir()
    for path in ("missing", "d", "/abs", "../x", ""):
        assert safefs.try_read(root, path, limit=10) is None
    (root / "ok").write_text("yes")
    assert safefs.try_read(root, "ok", limit=10) == b"yes"


# --- writing -------------------------------------------------------------------------


def test_write_creates_directories_with_open_permissions(root: Path) -> None:
    assert safefs.write_bytes(root, "a/b/c.txt", b"hello") == 5
    assert (root / "a/b/c.txt").read_bytes() == b"hello"
    assert stat.S_IMODE((root / "a").stat().st_mode) == 0o777
    assert stat.S_IMODE((root / "a/b").stat().st_mode) == 0o777
    assert stat.S_IMODE((root / "a/b/c.txt").stat().st_mode) & 0o666 == 0o666


def test_write_truncates_and_keeps_the_executable_bit(root: Path) -> None:
    script = root / "run.sh"
    script.write_text("a long original body\n")
    script.chmod(0o755)
    safefs.write_bytes(root, "run.sh", b"short\n")
    assert script.read_bytes() == b"short\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o777


def test_write_through_a_final_link_is_refused(root: Path, outside: Path) -> None:
    (root / "link").symlink_to(outside / "secret.txt")
    with pytest.raises(LinkInPathError):
        safefs.write_bytes(root, "link", b"overwritten")
    assert (outside / "secret.txt").read_text() == "host secret\n"


def test_replace_links_swaps_the_link_for_a_file(root: Path, outside: Path) -> None:
    (root / "link").symlink_to(outside / "secret.txt")
    safefs.write_bytes(root, "link", b"new", replace_links=True)
    assert not (root / "link").is_symlink()
    assert (root / "link").read_bytes() == b"new"
    assert (outside / "secret.txt").read_text() == "host secret\n"


def test_write_onto_a_directory_is_refused(root: Path) -> None:
    (root / "d").mkdir()
    with pytest.raises(NotRegularFileError):
        safefs.write_bytes(root, "d", b"x")


# --- races: a parent swapped mid-operation --------------------------------------------


@pytest.fixture
def swap_after_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, Callable[[], None]], None]:
    """After ``os.open`` opens a directory component called ``name``, run ``swap``."""

    def install(name: str, swap: Callable[[], None]) -> None:
        real_open = os.open
        fired = False

        def spy(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal fired
            fd = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
            if not fired and path == name and flags & os.O_DIRECTORY:
                fired = True
                swap()
            return fd

        monkeypatch.setattr(os, "open", spy)

    return install


def test_parent_swapped_for_a_link_after_it_was_opened_cannot_redirect_a_write(
    root: Path, outside: Path, swap_after_opening: Callable[[str, Callable[[], None]], None]
) -> None:
    (root / "a" / "b").mkdir(parents=True)

    def swap() -> None:
        (root / "a").rename(root / "a_old")
        (root / "a").symlink_to(outside)

    swap_after_opening("a", swap)
    safefs.write_bytes(root, "a/b/c.txt", b"payload")
    # The walk kept the descriptor of the real directory, so the write landed there.
    assert (root / "a_old/b/c.txt").read_bytes() == b"payload"
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


def test_inner_directory_swapped_before_it_is_opened_is_refused(
    root: Path, outside: Path, swap_after_opening: Callable[[str, Callable[[], None]], None]
) -> None:
    (root / "a" / "b").mkdir(parents=True)

    def swap() -> None:
        (root / "a" / "b").rmdir()
        (root / "a" / "b").symlink_to(outside)

    swap_after_opening("a", swap)
    with pytest.raises(LinkInPathError):
        safefs.write_bytes(root, "a/b/c.txt", b"payload")
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


def test_parent_swapped_before_a_read_is_refused(
    root: Path, outside: Path, swap_after_opening: Callable[[str, Callable[[], None]], None]
) -> None:
    (root / "a" / "b").mkdir(parents=True)
    (root / "a/b/data.txt").write_text("inside")
    (outside / "data.txt").write_text("host file")

    def swap() -> None:
        (root / "a" / "b").rename(root / "b_old")
        (root / "a" / "b").symlink_to(outside)

    swap_after_opening("a", swap)
    with pytest.raises(LinkInPathError):
        safefs.read_bytes(root, "a/b/data.txt", limit=100)


# --- remove, mode, list --------------------------------------------------------------


def test_remove_files_and_links_but_never_directories_or_targets(root: Path, outside: Path) -> None:
    (root / "f.txt").write_text("x")
    (root / "link").symlink_to(outside / "secret.txt")
    (root / "d").mkdir()
    safefs.remove(root, "f.txt")
    safefs.remove(root, "link")
    assert not (root / "f.txt").exists()
    assert not (root / "link").is_symlink()
    assert (outside / "secret.txt").exists()
    with pytest.raises(NotRegularFileError):
        safefs.remove(root, "d")
    with pytest.raises(MissingPathError):
        safefs.remove(root, "f.txt")


def test_mode_of_does_not_follow_links(root: Path, outside: Path) -> None:
    (root / "link").symlink_to(outside / "secret.txt")
    assert stat.S_ISLNK(safefs.mode_of(root, "link"))
    with pytest.raises(MissingPathError):
        safefs.mode_of(root, "nothing")


def test_list_dir_reports_kinds_without_following(root: Path, outside: Path) -> None:
    (root / "f.txt").write_text("x")
    (root / "d").mkdir()
    (root / "link").symlink_to(outside)
    os.mkfifo(root / "pipe")
    entries = {entry.name: entry.kind for entry in safefs.list_dir(root, ".")}
    assert entries == {
        "d": EntryKind.DIR,
        "f.txt": EntryKind.FILE,
        "link": EntryKind.SYMLINK,
        "pipe": EntryKind.OTHER,
    }
    with pytest.raises(LinkInPathError):
        safefs.list_dir(root, "link")


# --- walking -------------------------------------------------------------------------


def make_tree(root: Path, outside: Path) -> None:
    for rel in ("b.py", "a/x.py", "a/deep/y.py", ".git/config", "node_modules/m.js"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    (root / "linkdir").symlink_to(outside)
    (root / "linkfile").symlink_to("b.py")
    os.mkfifo(root / "pipe")


def test_walk_lists_regular_files_and_skips_noise(root: Path, outside: Path) -> None:
    make_tree(root, outside)
    assert list(safefs.walk(root)) == ["a/deep/y.py", "a/x.py", "b.py"]
    assert list(safefs.walk(root, include_links=True)) == [
        "a/deep/y.py",
        "a/x.py",
        "b.py",
        "linkdir",
        "linkfile",
    ]


def test_directory_swapped_for_a_link_during_a_walk_is_not_entered(
    root: Path, outside: Path
) -> None:
    (root / "a").mkdir()
    (root / "a/first.py").write_text("1")
    (root / "z").mkdir()
    (root / "z/last.py").write_text("2")
    walker = safefs.walk(root)
    assert next(walker) == "a/first.py"
    (root / "z").rename(root / "z_moved")
    (root / "z").symlink_to(outside)
    rest = list(walker)
    assert "z/secret.txt" not in rest
    assert all(not path.startswith("z/") for path in rest)


# --- copying -------------------------------------------------------------------------


def copy(
    source: Path, target: Path, leave_out: Callable[[str], bool] = lambda _: False
) -> safefs.CopyReport:
    return safefs.copy_tree(source, target, leave_out=leave_out, max_files=100, max_bytes=10_000)


def test_copy_tree_copies_files_and_links_as_links(tmp_path: Path, outside: Path) -> None:
    source, target = tmp_path / "src", tmp_path / "dst"
    target.mkdir()
    (source / "pkg").mkdir(parents=True)
    (source / "pkg/a.py").write_text("print(1)\n")
    (source / "run.sh").write_text("echo\n")
    (source / "run.sh").chmod(0o755)
    (source / "to_outside").symlink_to(outside / "secret.txt")
    (source / ".git").mkdir()
    (source / ".git/config").write_text("[core]\n")
    os.mkfifo(source / "pipe")

    report = copy(source, target)

    assert (target / "pkg/a.py").read_text() == "print(1)\n"
    assert (target / "to_outside").is_symlink()
    assert (target / "to_outside").readlink() == outside / "secret.txt"
    assert not (target / ".git").exists()
    assert not (target / "pipe").exists()
    assert stat.S_IMODE((target / "run.sh").stat().st_mode) & 0o111
    assert stat.S_IMODE((target / "pkg").stat().st_mode) == 0o777
    assert (report.files, report.links, report.skipped) == (2, 1, ["pipe"])


def test_copy_tree_leaves_out_what_the_predicate_names(tmp_path: Path) -> None:
    source, target = tmp_path / "src", tmp_path / "dst"
    target.mkdir()
    (source / ".ssh").mkdir(parents=True)
    (source / ".ssh/id_rsa").write_text("key")
    (source / ".env").write_text("TOKEN=x")
    (source / "env_link").symlink_to(".env")
    (source / "app.py").write_text("ok")

    names = {".ssh", ".env", "env_link"}
    report = copy(source, target, leave_out=lambda rel: rel in names)

    assert sorted(p.name for p in target.iterdir()) == ["app.py"]
    assert sorted(report.left_out) == [".env", ".ssh", "env_link"]


def test_copy_tree_enforces_its_limits(tmp_path: Path) -> None:
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "big.bin").write_bytes(b"x" * 20_000)
    with pytest.raises(LimitExceededError):
        copy(source, target)
    for index in range(5):
        (source / f"f{index}").write_text("x")
    (source / "big.bin").unlink()
    with pytest.raises(LimitExceededError):
        safefs.copy_tree(source, tmp_path, leave_out=lambda _: False, max_files=3, max_bytes=10_000)


def test_refusals_never_name_host_paths(root: Path, outside: Path) -> None:
    (root / "sub").symlink_to(outside)
    caught: list[str] = []
    for action in (
        lambda: safefs.read_bytes(root, "sub/secret.txt", limit=10),
        lambda: safefs.read_bytes(root, "missing.txt", limit=10),
        lambda: safefs.write_bytes(root, "sub/x", b"x"),
    ):
        with pytest.raises(FileRefusedError) as info:
            action()
        caught.append(str(info.value))
    for message in caught:
        assert str(root) not in message
        assert str(outside) not in message
