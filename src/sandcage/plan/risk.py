"""The harness's own risk floor for a manifest. The planner rates its plan too, but that
rating is model output, so the plan's risk never falls below what the manifest itself
implies."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from sandcage.contract.common import Manifest, Risk
from sandcage.policy.globs import glob_match

_ORDER: Final = (Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL)
DELETE_PROGRAMS: Final = frozenset({"rm", "unlink", "rmdir", "shred"})
# Sample paths of configuration, build and CI files: a write glob that reaches any of them
# can change how the project is built, tested or deployed.
CONFIG_SAMPLES: Final = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "pytest.ini",
    "tox.ini",
    "conftest.py",
    "tests/conftest.py",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    ".gitignore",
    ".github/workflows/ci.yml",
)


def rank(risk: Risk) -> int:
    return _ORDER.index(risk)


def highest(risks: Iterable[Risk]) -> Risk:
    return max(risks, key=rank, default=Risk.LOW)


def _reaches(patterns: list[str], path: str) -> bool:
    return any(glob_match(pattern, path) for pattern in patterns)


def manifest_risks(manifest: Manifest, suspicious: Iterable[str] = ()) -> list[tuple[Risk, str]]:
    """Every (minimum risk, why) the manifest implies."""
    found: list[tuple[Risk, str]] = []
    if manifest.network:
        found.append((Risk.HIGH, f"network access to {', '.join(manifest.network)}"))
    deletes = sorted(set(manifest.commands) & DELETE_PROGRAMS)
    if deletes:
        found.append((Risk.HIGH, f"it may delete files ({', '.join(deletes)})"))
    config = [path for path in CONFIG_SAMPLES if _reaches(manifest.write_paths, path)]
    if config:
        found.append((Risk.HIGH, f"the write paths reach configuration files such as {config[0]}"))
    readable = manifest.read_paths + manifest.write_paths
    for path in suspicious:
        if _reaches(manifest.write_paths, path):
            found.append((Risk.HIGH, f"the write paths reach {path}, which the scout flagged"))
        elif _reaches(readable, path):
            found.append((Risk.MEDIUM, f"the read paths reach {path}, which the scout flagged"))
    if manifest.may_edit_tests:
        found.append((Risk.MEDIUM, "it may edit tests"))
    return found


def risk_floor(manifest: Manifest, suspicious: Iterable[str] = ()) -> Risk:
    return highest(risk for risk, _ in manifest_risks(manifest, suspicious))


def raise_risk(
    claimed: Risk | None, manifest: Manifest, suspicious: Iterable[str] = ()
) -> tuple[Risk, list[str]]:
    """The plan's risk after the floor, with the reasons people see.

    A missing or unknown claimed risk counts as high. When the floor raises the claim, the
    first reason says so ("raised from low to high by the harness: ...").
    """
    found = manifest_risks(manifest, suspicious)
    reasons = [why for _, why in found]
    if claimed is None:
        return Risk.HIGH, ["the planner gave no valid risk, so it counts as high", *reasons]
    floor = highest(risk for risk, _ in found)
    if rank(floor) <= rank(claimed):
        return claimed, reasons
    drivers = [why for risk, why in found if risk is floor]
    raised = f"raised from {claimed.value} to {floor.value} by the harness: {'; '.join(drivers)}"
    return floor, [raised, *[why for why in reasons if why not in drivers]]
