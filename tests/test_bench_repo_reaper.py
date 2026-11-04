"""A killed API leaves scenario repos behind (its run thread never reached the finally that
removes them). The next start removes the stale ones, and only those: found when the compose
stack's API process was killed mid-run in the local rehearsal."""

from __future__ import annotations

import os
from pathlib import Path

from sandcage.bench.catalogue import SCENARIOS
from sandcage.bench.repo import REPO_PREFIX, STALE_SECONDS, materialize, reap_stale_repos

NOW = 1_800_000_000.0


def aged(path: Path, seconds: float) -> None:
    stamp = NOW - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def leftover(root: Path, name: str, age: float) -> Path:
    path = root / name
    path.mkdir()
    (path / "README.md").write_text("x", encoding="utf-8")
    aged(path, age)
    return path


def test_a_stale_scenario_repo_is_removed(tmp_path: Path) -> None:
    stale = leftover(tmp_path, REPO_PREFIX + "abcd1234", STALE_SECONDS + 60)
    assert reap_stale_repos(tmp_path, clock=lambda: NOW) == [stale.name]
    assert not stale.exists()


def test_a_repo_a_live_run_may_still_use_is_kept(tmp_path: Path) -> None:
    fresh = leftover(tmp_path, REPO_PREFIX + "abcd1234", 60)
    assert reap_stale_repos(tmp_path, clock=lambda: NOW) == []
    assert fresh.exists()


def test_other_names_files_and_links_are_never_touched(tmp_path: Path) -> None:
    other = leftover(tmp_path, "sandcage-abcd1234", STALE_SECONDS + 60)  # a run dir, not ours
    wrong_length = leftover(tmp_path, REPO_PREFIX + "short", STALE_SECONDS + 60)
    shouty = leftover(tmp_path, REPO_PREFIX + "ABCD1234", STALE_SECONDS + 60)
    a_file = tmp_path / (REPO_PREFIX + "file1234")
    a_file.write_text("not a directory", encoding="utf-8")
    aged(a_file, STALE_SECONDS + 60)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / (REPO_PREFIX + "link1234")
    link.symlink_to(precious)
    aged(link, STALE_SECONDS + 60)
    assert reap_stale_repos(tmp_path, clock=lambda: NOW) == []
    for kept in (other, wrong_length, shouty, a_file, link, precious / "keep.txt"):
        assert kept.exists() or kept.is_symlink(), kept


def test_a_link_inside_a_stale_repo_is_removed_without_following_it(tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep", encoding="utf-8")
    stale = leftover(tmp_path, REPO_PREFIX + "abcd1234", STALE_SECONDS + 60)
    (stale / "inner-link").symlink_to(precious)
    aged(stale, STALE_SECONDS + 60)
    assert reap_stale_repos(tmp_path, clock=lambda: NOW) == [stale.name]
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_a_missing_temp_root_is_not_an_error(tmp_path: Path) -> None:
    assert reap_stale_repos(tmp_path / "absent", clock=lambda: NOW) == []


def test_what_materialize_creates_matches_what_the_reaper_recognises(tmp_path: Path) -> None:
    repo = materialize(SCENARIOS[0], tmp_path)
    aged(repo, STALE_SECONDS + 60)
    assert reap_stale_repos(tmp_path, clock=lambda: NOW) == [repo.name]
    assert not repo.exists()
