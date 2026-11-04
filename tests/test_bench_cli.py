"""The Bench command line: it writes the results file and Markdown, labels them "scoring
check", and exits non-zero when the check does not come out as designed or Docker is down."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandcage.bench.__main__ import main
from sandcage.sandbox.docker_api import DockerError
from sandcage.sandbox.run import RunnerFactory
from tests.bench_helpers import BenchRunner


def host() -> tuple[RunnerFactory, str | None]:
    return BenchRunner, "host runner (tests)"


def test_it_writes_the_results_and_the_markdown(tmp_path: Path) -> None:
    lines: list[str] = []
    code = main(
        ["--only", "rollback,failure_rm", "--out", str(tmp_path / "out")],
        out=lines.append,
        runner=host,
    )
    assert code == 0, lines
    data = json.loads((tmp_path / "out" / "results.json").read_text())
    assert data["label"] == "scoring check"
    assert (data["provider"], data["model"], data["scripted"]) == ("fake", "scripted", True)
    assert [s["id"] for s in data["scenarios"]] == ["failure_rm", "rollback"]
    markdown = (tmp_path / "out" / "results.md").read_text()
    assert markdown.startswith("# Sandcage Bench: scoring check")
    assert "not real results" in markdown
    assert any("scoring check: 2 scenarios, 0 errors" in line for line in lines)


def test_one_mode_only(tmp_path: Path) -> None:
    code = main(
        ["--only", "rollback", "--mode", "baseline", "--out", str(tmp_path)],
        out=lambda _line: None,
        runner=host,
    )
    data = json.loads((tmp_path / "results.json").read_text())
    scenario = data["scenarios"][0]
    assert scenario["baseline"] is not None
    assert scenario["full"] is None
    assert code == 0


def test_an_unknown_scenario_is_refused(tmp_path: Path) -> None:
    lines: list[str] = []
    assert main(["--only", "nope", "--out", str(tmp_path)], out=lines.append, runner=host) == 2
    assert lines == ["unknown scenario: nope"]
    assert list(tmp_path.iterdir()) == []


def test_without_docker_it_says_so_and_writes_nothing(tmp_path: Path) -> None:
    def down() -> tuple[RunnerFactory, str | None]:
        raise DockerError("cannot reach the engine")

    lines: list[str] = []
    assert main(["--out", str(tmp_path)], out=lines.append, runner=down) == 2
    assert lines == ["the Bench needs Docker: cannot reach the engine"]
    assert list(tmp_path.iterdir()) == []


def test_a_check_that_does_not_come_out_as_designed_exits_non_zero(tmp_path: Path) -> None:
    def broken() -> tuple[RunnerFactory, str | None]:
        def factory(_workspace: Path) -> BenchRunner:
            raise OSError("no sandbox")

        return factory, None

    lines: list[str] = []
    code = main(["--only", "rollback", "--out", str(tmp_path)], out=lines.append, runner=broken)
    assert code == 1
    data = json.loads((tmp_path / "results.json").read_text())
    assert data["errors"] == 2
    assert any("did not come out as designed" in line for line in lines)


def test_the_real_provider_is_not_offered_here() -> None:
    with pytest.raises(SystemExit):
        main(["--provider", "vultr"], runner=host)
