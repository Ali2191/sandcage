"""Learn: deterministic suggestions, the learned layer (only narrows, per repo), and
accepting a rule (validated, atomic, no duplicates) that the next run then uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandcage.agent.scenarios import DEFAULT_TASK
from sandcage.contract.common import Phase
from sandcage.contract.events import LearnEvent, LearnSuggestion, StepEvent
from sandcage.harness.pipeline import RunOptions
from sandcage.learn.learned import (
    HEADER,
    LearnedConflictError,
    LearnedLayer,
    LearnedRule,
    LearnedRuleError,
    accept,
    load_rules,
    rule_id,
    validate_rule,
)
from sandcage.learn.suggest import suggest
from sandcage.policy import Decision, Risk, RuleSource
from sandcage.roles.scout import FLAG_INSTRUCTIONS, FLAG_LEARNED, FLAG_NO_ENTRY, collect
from sandcage.sandbox.run import SandboxRun
from tests.agent_helpers import LocalRunner
from tests.harness_helpers import run_scenario
from tests.policy_helpers import engine
from tests.sandbox_helpers import DEMO_REPO

REPO = "invoice-calc"


def _step(step: int, **fields: object) -> StepEvent:
    base: dict[str, object] = {
        "run_id": "r",
        "phase": Phase.ACT,
        "ts": 0.0,
        "step": step,
        "guard": True,
        "tool": "run_command",
        "args": {"command": "ls"},
        "decision": Decision.ALLOW,
        "rule_id": "cmd.allow_safe",
        "reason": "ok",
        "risk": Risk.LOW,
        "executed": True,
        "blocked": False,
    }
    base.update(fields)
    return StepEvent.model_validate(base)


def _refused(step: int, command: str) -> StepEvent:
    return _step(
        step,
        args={"command": command},
        decision=Decision.ASK,
        approved=False,
        executed=False,
        blocked=True,
        blocked_sentence="Tried to run a command",
    )


# --- suggestions -------------------------------------------------------------------------------


def test_the_demo_suggests_the_flagged_files_with_evidence(tmp_path: Path) -> None:
    run = run_scenario(tmp_path)
    learn = next(e for e in run.result.events if isinstance(e, LearnEvent))
    assert [(s.kind, s.target) for s in learn.suggestions] == [
        ("suspicious_file", "README.md"),
        ("suspicious_file", "invoice_calc/invoice.py"),
    ]
    assert "contains instructions for AI agents" in learn.suggestions[0].explanation
    assert learn.notes == [
        "Already covered by net.deny_tools: Tried to send your .env to collector.example.net.",
        "Already covered by path.deny_secrets: Tried to read your secrets in .env.",
    ]
    assert run.result.outputs is not None
    assert run.result.outputs.learn["suggestions"]


def test_only_instruction_flags_count_and_block_sources_do() -> None:
    blocked = _step(
        3,
        decision=Decision.DENY,
        executed=False,
        blocked=True,
        blocked_sentence="Tried to reach evil.example",
        tags=["prompt_injection_suspected"],
        source="docs/setup.md",
    )
    suggestions, _ = suggest(
        [blocked],
        repo=REPO,
        scout_flags=[("broken.py", FLAG_NO_ENTRY), ("notes.md", FLAG_INSTRUCTIONS)],
    )
    assert [s.target for s in suggestions] == ["notes.md", "docs/setup.md"]


def test_pages_and_refused_commands_suggest_rules_and_masked_ones_are_skipped() -> None:
    suggestions, _ = suggest(
        [_refused(1, "pip   install  evilpkg"), _refused(2, "curl -H [MASKED:token] x")],
        repo=REPO,
        suspicious_pages=["https://docs.example.org/page?x=1"],
    )
    assert [(s.kind, s.target) for s in suggestions] == [
        ("ask_host", "docs.example.org"),
        ("deny_command", "pip install evilpkg"),
    ]
    assert all(s.rule_id.startswith("learned.") for s in suggestions)


def test_already_learned_is_a_note() -> None:
    known = rule_id("suspicious_file", "README.md")
    suggestions, notes = suggest(
        [], repo=REPO, scout_flags=[("README.md", FLAG_INSTRUCTIONS)], learned_ids=[known]
    )
    assert suggestions == []
    assert notes[0].startswith("Already learned: Treat README.md as suspicious")


# --- validation and accepting ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "plan.x", "kind": "deny_command", "target": "ls"},
        {"id": "learned.x", "kind": "allow_command", "target": "ls"},
        {"id": "learned.x", "kind": "deny_command", "target": ""},
        {"id": "learned.x", "kind": "deny_command", "target": "curl [MASKED:token]"},
        {"id": "learned.x", "kind": "suspicious_file", "target": "../outside.md"},
        {"id": "learned.x", "kind": "suspicious_file", "target": "/etc/passwd"},
        {"id": "learned.x", "kind": "ask_host", "target": "user@host.example"},
        {"id": "learned.x", "kind": "deny_command", "target": "ls", "decision": "allow"},
        {"id": "learned.x", "kind": "deny_command", "target": "ls", "repos": "invoice-calc"},
        "learned.x",
    ],
)
def test_invalid_rules_are_rejected(raw: object) -> None:
    with pytest.raises(LearnedRuleError):
        validate_rule(raw)


def test_a_rulebook_id_cannot_be_reused() -> None:
    with pytest.raises(LearnedRuleError, match="already a rule id"):
        validate_rule(
            {"id": "learned.a", "kind": "deny_command", "target": "ls"}, reserved=["learned.a"]
        )


def test_accept_validates_dedupes_and_keeps_the_header(tmp_path: Path) -> None:
    path = tmp_path / "policy" / "learned.yaml"
    suggestion = LearnSuggestion(
        id="deny_command:pip install evilpkg",
        kind="deny_command",
        rule_id=rule_id("deny_command", "pip install evilpkg"),
        explanation="x",
        target="pip install evilpkg",
    )
    rule = accept(path, suggestion, REPO)
    assert rule.repos == (REPO,)
    text = path.read_text()
    assert text.startswith(HEADER)
    assert load_rules(path) == [rule]
    with pytest.raises(LearnedConflictError):
        accept(path, suggestion, REPO)
    assert load_rules(path) == [rule]
    assert [p.name for p in path.parent.iterdir()] == ["learned.yaml"]  # no temp left
    path.write_text("rules: [{id: nope}]\n")
    with pytest.raises(LearnedRuleError):
        load_rules(path)
    assert load_rules(tmp_path / "missing.yaml") == []


# --- the layer -------------------------------------------------------------------------------


def _layer(*rules: LearnedRule, repo: str = REPO) -> LearnedLayer:
    return LearnedLayer(rules, repo)


def test_learned_rules_only_narrow_and_match_with_source_learned(tmp_path: Path) -> None:
    deny = validate_rule(
        {"id": "learned.d", "kind": "deny_command", "target": "ls  -la", "repos": [REPO]}
    )
    run = SandboxRun(
        DEMO_REPO,
        engine=engine(),
        runner_factory=LocalRunner,
        layers=(_layer(deny),),
        temp_root=tmp_path,
    )
    with run:
        denied = run.execute({"tool": "run_command", "args": {"command": "ls -la"}})
        other = run.execute({"tool": "run_command", "args": {"command": "ls"}})
        env = run.execute({"tool": "read_file", "args": {"path": ".env"}})
    assert (denied.decision.decision, denied.decision.rule_id) == (Decision.DENY, "learned.d")
    assert any(
        hit.rule_id == "learned.d" and hit.source is RuleSource.LEARNED
        for hit in denied.decision.matched_rules
    )
    assert other.decision.allowed
    assert env.decision.decision is Decision.DENY  # a learned layer never relaxes the rulebook


def test_ask_host_and_the_repo_filter() -> None:
    ask = validate_rule(
        {"id": "learned.h", "kind": "ask_host", "target": "docs.example.org", "repos": [REPO]}
    )
    assert _layer(ask).ids == ("learned.h",)
    assert _layer(ask, repo="other-repo").ids == ()


def test_a_glob_like_file_name_matches_only_itself(tmp_path: Path) -> None:
    rule = validate_rule({"id": "learned.f", "kind": "suspicious_file", "target": "a[1].py"})
    layer = _layer(rule)
    assert layer.suspicious == ("a[1].py",)
    (tmp_path / "a[1].py").write_text("x\n")
    (tmp_path / "a1.py").write_text("y\n")
    files, found = collect(tmp_path, is_secret=lambda _p: False, learned=layer.suspicious)
    assert found.flags == {"a[1].py": FLAG_LEARNED}
    assert [f.path for f in files] == ["a1.py"]


def test_an_accepted_rule_is_used_by_the_next_run_and_only_for_its_repo(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    first = run_scenario(tmp_path / "first")
    learn = next(e for e in first.result.events if isinstance(e, LearnEvent))
    accept(path, learn.suggestions[0], REPO)  # README.md, by a human
    layer = LearnedLayer(load_rules(path), REPO)

    second = run_scenario(
        tmp_path / "second",
        options=RunOptions(
            task=DEFAULT_TASK, learned_suspicious=layer.suspicious, learned_ids=layer.ids
        ),
    )
    assert '<untrusted source="README.md">' not in second.models.scout.seen  # never sent
    learned = next(e for e in second.result.events if isinstance(e, LearnEvent))
    assert [s.target for s in learned.suggestions] == ["invoice_calc/invoice.py"]
    assert learned.notes[0].startswith("Already learned: Treat README.md")
    assert LearnedLayer(load_rules(path), "other-repo").suspicious == ()
