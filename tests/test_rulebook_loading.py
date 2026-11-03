"""The rulebook loader rejects every malformed input loudly, with the place it was found."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sandcage.policy import DEFAULT_RULEBOOK_PATH, PolicyError, load_rulebook
from sandcage.policy.rulebook import parse_rulebook, parse_rulebook_text


def base() -> dict[str, object]:
    data = yaml.safe_load(DEFAULT_RULEBOOK_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def rule(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "test.rule",
        "tools": ["read_file"],
        "match": {"always": True},
        "decision": "allow",
        "risk": "low",
        "reason": "A test rule.",
    }
    item.update(overrides)
    return item


def with_rules(*rules: object) -> dict[str, object]:
    data = base()
    data["rules"] = list(rules)
    return data


def test_default_rulebook_path_resolves_and_loads() -> None:
    assert DEFAULT_RULEBOOK_PATH.name == "rulebook.yaml"
    assert DEFAULT_RULEBOOK_PATH.is_file()
    assert len(load_rulebook().rules) == 22


def test_missing_file_is_a_policy_error(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read the rulebook"):
        load_rulebook(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("bad_rule", "message"),
    [
        (rule(id=None), r"rules\[0\]\.id"),
        (rule(id="NoDots"), r"rules\[0\]\.id"),
        (rule(decision="maybe"), r"decision: must be allow, ask or deny"),
        (rule(risk="extreme"), r"risk: must be low"),
        (rule(reason=""), r"reason"),
        (rule(match={}), r"at least one matcher"),
        (rule(match=None), r"at least one matcher"),
        (rule(match={"teleport": True}), r"unknown matcher 'teleport'"),
        (rule(match={"always": False}), r"'always' takes the value true"),
        (rule(match={"path_glob": []}), r"non-empty list"),
        (rule(match={"bulk_delete": 0}), r"whole number"),
        (rule(match={"url_host_in": ["bad host!"]}), r"invalid host name"),
        (rule(tools=["read_file"], match={"program_in": ["curl"]}), r"cannot judge"),
        (rule(tools=["teleport"]), r"unknown tool"),
        (rule(tools=[]), r"tools: must be a non-empty list"),
        (rule(id="plan.sneaky"), r"reserved prefix"),
        (rule(id="learned.sneaky"), r"reserved prefix"),
        (rule(extra="x"), r"unknown key"),
        ("not a rule", r"must be a mapping"),
    ],
)
def test_invalid_rules_are_rejected(bad_rule: object, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        parse_rulebook(with_rules(bad_rule))


def test_duplicate_rule_ids_are_rejected() -> None:
    with pytest.raises(PolicyError, match="used more than once"):
        parse_rulebook(with_rules(rule(), rule()))


@pytest.mark.parametrize("rule_id", ["default.deny", "action.invalid", "diff.deny_secrets"])
def test_ids_may_not_collide_with_builtin_or_diff_rules(rule_id: str) -> None:
    with pytest.raises(PolicyError, match="used more than once"):
        parse_rulebook(with_rules(rule(id=rule_id)))


@pytest.mark.parametrize("window", [0, -1, True, "5", 101, None])
def test_bad_taint_window_is_rejected(window: object) -> None:
    data = base()
    taint = data["taint"]
    assert isinstance(taint, dict)
    taint["window"] = window
    with pytest.raises(PolicyError, match=r"taint\.window"):
        parse_rulebook(data)


@pytest.mark.parametrize("known", [[], "pytest", [""], [3]])
def test_bad_known_commands_are_rejected(known: object) -> None:
    data = base()
    taint = data["taint"]
    assert isinstance(taint, dict)
    taint["known_commands"] = known
    with pytest.raises(PolicyError, match=r"taint\.known_commands"):
        parse_rulebook(data)


def test_untrusted_tools_are_limited_to_content_readers() -> None:
    data = base()
    taint = data["taint"]
    assert isinstance(taint, dict)
    taint["untrusted_tools"] = ["read_file", "finish"]
    with pytest.raises(PolicyError, match="untrusted_tools"):
        parse_rulebook(data)


def test_invalid_patterns_are_policy_errors() -> None:
    data = base()
    guard = data["diff_guard"]
    assert isinstance(guard, dict)
    guard["network_exec"] = [{"name": "broken", "pattern": "(unclosed"}]
    with pytest.raises(PolicyError, match="invalid regular expression"):
        parse_rulebook(data)
    data = base()
    secrets = data["secrets"]
    assert isinstance(secrets, dict)
    secrets["name_pattern"] = "[a-"
    with pytest.raises(PolicyError, match=r"secrets\.name_pattern"):
        parse_rulebook(data)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("version", 2, "version"),
        ("workspace", "relative", "workspace"),
        ("workspace", "/", "workspace"),
        ("safe_paths", ["dev/null"], "absolute path"),
        ("test_files", [], "test_files"),
        ("package_managers", ["pip"], "package_managers"),
    ],
)
def test_bad_sections_are_rejected(key: str, value: object, message: str) -> None:
    data = base()
    data[key] = value
    with pytest.raises(PolicyError, match=message):
        parse_rulebook(data)


def test_unknown_and_missing_top_level_keys_are_rejected() -> None:
    data = base()
    data["rulez"] = []
    with pytest.raises(PolicyError, match="unknown key"):
        parse_rulebook(data)
    data = base()
    del data["taint"]
    with pytest.raises(PolicyError, match="missing taint"):
        parse_rulebook(data)


def test_duplicate_yaml_keys_are_rejected() -> None:
    text = DEFAULT_RULEBOOK_PATH.read_text(encoding="utf-8") + "\nversion: 1\n"
    with pytest.raises(PolicyError, match="duplicate key"):
        parse_rulebook_text(text)


def test_invalid_yaml_is_a_policy_error() -> None:
    with pytest.raises(PolicyError, match="not valid YAML"):
        parse_rulebook_text("rules: [unclosed")


def test_empty_rules_list_is_valid() -> None:
    assert parse_rulebook(with_rules()).rules == ()
