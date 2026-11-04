"""Every scripted role reply, in every scenario, parses with its role's own parser, so the
scripts in ``agent/scenarios.py`` and the parsers cannot drift apart."""

from __future__ import annotations

import pytest

from sandcage.agent.scenarios import ScenarioId, scripted_models
from sandcage.roles.reader import parse_answer
from sandcage.roles.redteam import parse_findings
from sandcage.roles.scout import parse_batch
from sandcage.roles.understand import parse_understanding
from sandcage.roles.verify import parse_verdict
from tests.sandbox_helpers import DEMO_REPO


def _replies(scenario: ScenarioId, role: str) -> list[str]:
    client = scripted_models(scenario).roles[role]
    return [*client.replies, *([client.when_done] if client.when_done else [])]


@pytest.mark.parametrize("scenario", list(ScenarioId))
def test_every_scripted_role_reply_parses(scenario: ScenarioId) -> None:
    demo_files = [p.relative_to(DEMO_REPO).as_posix() for p in DEMO_REPO.rglob("*") if p.is_file()]
    for reply in _replies(scenario, "scout"):
        assert len(parse_batch(reply, paths=demo_files)) == 6
    for reply in _replies(scenario, "understand"):
        parse_understanding(reply)
    for reply in _replies(scenario, "verifier"):
        parse_verdict(reply)
    for reply in _replies(scenario, "redteam"):
        parse_findings(reply)
    for reply in _replies(scenario, "reader"):
        parse_answer(reply)
    assert _replies(scenario, "verifier")
    assert _replies(scenario, "redteam")
    assert _replies(scenario, "reader")
