"""Load and validate the rulebook: a human-readable YAML file of ordered rules.

Every problem raises :class:`PolicyError` with the place it was found, at load time:
unknown sections or keys, bad decisions or risks, empty or unknown matchers, matchers
paired with tools they cannot judge, duplicate ids, invalid patterns, a bad taint window.
A rulebook with no rules is valid and denies everything.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

import yaml

from sandcage.policy.actions import ALL_TOOLS, Tool
from sandcage.policy.diff_guard import DiffGuardConfig, NetworkExecPattern
from sandcage.policy.matchers import MatchContext, Matcher, Subject, build_matcher
from sandcage.policy.paths import SecretFiles, Workspace
from sandcage.policy.types import Decision, PolicyError, Risk, RuleSpec

DEFAULT_RULEBOOK_PATH: Final = Path(__file__).with_name("rulebook.yaml")

ACTION_INVALID: Final = RuleSpec(
    "action.invalid",
    Decision.DENY,
    Risk.HIGH,
    "The action is malformed, so it is blocked.",
)
DEFAULT_DENY: Final = RuleSpec(
    "default.deny",
    Decision.DENY,
    Risk.HIGH,
    "No rule allows this action, so it is blocked.",
)
TAINT_ESCALATE: Final = RuleSpec(
    "taint.escalate",
    Decision.ASK,
    Risk.HIGH,
    "Untrusted content was read moments ago, so this risky action needs a human.",
)
BUILTIN_RULES: Final = (ACTION_INVALID, DEFAULT_DENY, TAINT_ESCALATE)

_RULE_ID: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
_RESERVED_PREFIXES: Final = ("plan.", "learned.", "taint.")
_UNTRUSTED_TOOLS: Final = frozenset({Tool.READ_FILE, Tool.HTTP_GET})
_TOP_KEYS: Final = frozenset(
    {
        "version",
        "workspace",
        "secrets",
        "safe_paths",
        "test_files",
        "delete_programs",
        "package_managers",
        "taint",
        "diff_guard",
        "rules",
    }
)


@dataclass(frozen=True)
class Rule:
    spec: RuleSpec
    tools: frozenset[str]
    matchers: tuple[Matcher, ...]

    def matches(self, subject: Subject, ctx: MatchContext) -> bool:
        return subject.action.tool in self.tools and all(
            matcher.matches(subject, ctx) for matcher in self.matchers
        )


@dataclass(frozen=True)
class TaintSettings:
    tag: str
    window: int
    untrusted_tools: frozenset[str]
    reader_programs: frozenset[str]
    known_commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Rulebook:
    context: MatchContext
    rules: tuple[Rule, ...]
    taint: TaintSettings
    diff_guard: DiffGuardConfig

    @property
    def all_rules(self) -> tuple[RuleSpec, ...]:
        """Every rule id the policy can return: rulebook, diff guard and built-ins."""
        return (*(rule.spec for rule in self.rules), *self.diff_guard.rules, *BUILTIN_RULES)


def load_rulebook(path: Path = DEFAULT_RULEBOOK_PATH) -> Rulebook:
    """Read and validate a rulebook file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as problem:
        raise PolicyError(f"cannot read the rulebook at {path}: {problem.strerror}") from None
    return parse_rulebook_text(text)


def parse_rulebook_text(text: str) -> Rulebook:
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as problem:
        raise PolicyError(f"the rulebook is not valid YAML: {problem}") from None
    return parse_rulebook(data)


def parse_rulebook(data: object) -> Rulebook:
    top = _mapping(data, "rulebook", required=_TOP_KEYS, allowed=_TOP_KEYS)
    if top["version"] != 1:
        raise PolicyError("version: must be 1")
    workspace = Workspace(_workspace_root(top["workspace"]))
    secrets = _secrets(top["secrets"])
    context = MatchContext(
        workspace=workspace,
        secrets=secrets,
        safe_paths=frozenset(_absolute_paths(top["safe_paths"], "safe_paths")),
        test_files=_strings(top["test_files"], "test_files"),
        delete_programs=frozenset(_strings(top["delete_programs"], "delete_programs")),
        package_managers=_package_managers(top["package_managers"]),
    )
    diff_guard = _diff_guard(top["diff_guard"])
    rules = _rules(top["rules"])
    ids = [rule.spec.rule_id for rule in rules] + [spec.rule_id for spec in diff_guard.rules]
    builtin_ids = {spec.rule_id for spec in BUILTIN_RULES}
    seen: set[str] = set()
    for rule_id in ids:
        if rule_id in seen or rule_id in builtin_ids:
            raise PolicyError(f"rule id '{rule_id}' is used more than once")
        seen.add(rule_id)
    return Rulebook(context=context, rules=rules, taint=_taint(top["taint"]), diff_guard=diff_guard)


# --- sections ------------------------------------------------------------------------


def _workspace_root(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith("/") or value.strip("/") == "":
        raise PolicyError("workspace: must be an absolute directory other than /")
    return PurePosixPath(value.rstrip("/"))


def _secrets(value: object) -> SecretFiles:
    section = _mapping(value, "secrets", required={"globs", "name_pattern"})
    pattern = section["name_pattern"]
    if not isinstance(pattern, str) or not pattern:
        raise PolicyError("secrets.name_pattern: must be a regular expression")
    return SecretFiles(
        globs=_strings(section["globs"], "secrets.globs"),
        name_pattern=_compile(pattern, "secrets.name_pattern"),
    )


def _package_managers(value: object) -> Mapping[str, tuple[tuple[str, ...], ...]]:
    section = _mapping(value, "package_managers")
    return {
        program: tuple(tuple(sub.split()) for sub in _strings(subs, f"package_managers.{program}"))
        for program, subs in section.items()
    }


def _taint(value: object) -> TaintSettings:
    keys = {"tag", "window", "untrusted_tools", "reader_programs", "known_commands"}
    section = _mapping(value, "taint", required=keys)
    tag = section["tag"]
    if not isinstance(tag, str) or not tag:
        raise PolicyError("taint.tag: must be text")
    window = section["window"]
    if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 100:
        raise PolicyError("taint.window: must be a whole number from 1 to 100")
    untrusted = frozenset(_strings(section["untrusted_tools"], "taint.untrusted_tools"))
    if not untrusted <= _UNTRUSTED_TOOLS:
        raise PolicyError("taint.untrusted_tools: only read_file and http_get read content")
    return TaintSettings(
        tag=tag,
        window=window,
        untrusted_tools=untrusted,
        reader_programs=frozenset(_strings(section["reader_programs"], "taint.reader_programs")),
        known_commands=tuple(
            tuple(command.split())
            for command in _strings(section["known_commands"], "taint.known_commands")
        ),
    )


def _diff_guard(value: object) -> DiffGuardConfig:
    keys = {"code_files", "network_exec", "test_config_files", "rules"}
    section = _mapping(value, "diff_guard", required=keys)
    entries = section["network_exec"]
    if not isinstance(entries, list) or not entries:
        raise PolicyError("diff_guard.network_exec: must be a non-empty list")
    patterns = []
    for index, entry in enumerate(entries):
        where = f"diff_guard.network_exec[{index}]"
        item = _mapping(entry, where, required={"name", "pattern"})
        name, pattern = item["name"], item["pattern"]
        if not isinstance(name, str) or not name or not isinstance(pattern, str):
            raise PolicyError(f"{where}: needs a name and a pattern")
        patterns.append(NetworkExecPattern(name, _compile(pattern, f"{where}.pattern")))
    rule_keys = {"secrets", "network_exec", "weakened_tests"}
    rules = _mapping(section["rules"], "diff_guard.rules", required=rule_keys)
    return DiffGuardConfig(
        code_files=_strings(section["code_files"], "diff_guard.code_files"),
        network_exec=tuple(patterns),
        test_config_files=_strings(section["test_config_files"], "diff_guard.test_config_files"),
        secrets_rule=_spec(rules["secrets"], "diff_guard.rules.secrets"),
        network_exec_rule=_spec(rules["network_exec"], "diff_guard.rules.network_exec"),
        weakened_tests_rule=_spec(rules["weakened_tests"], "diff_guard.rules.weakened_tests"),
    )


def _rules(value: object) -> tuple[Rule, ...]:
    if not isinstance(value, list):
        raise PolicyError("rules: must be a list (it may be empty)")
    return tuple(_rule(item, f"rules[{index}]") for index, item in enumerate(value))


def _rule(value: object, where: str) -> Rule:
    keys = {"id", "tools", "match", "decision", "risk", "reason"}
    item = _mapping(value, where, required=keys)
    spec = _spec(item, where, allowed=keys)
    if spec.rule_id.startswith(_RESERVED_PREFIXES):
        raise PolicyError(f"{where}.id: '{spec.rule_id}' uses a reserved prefix")
    tools = frozenset(_strings(item["tools"], f"{where}.tools"))
    unknown = sorted(tools - ALL_TOOLS)
    if unknown:
        raise PolicyError(f"{where}.tools: unknown tool(s) {', '.join(unknown)}")
    match = item["match"]
    if not isinstance(match, dict) or not match:
        raise PolicyError(f"{where}.match: must name at least one matcher")
    matchers: list[Matcher] = []
    for name, param in match.items():
        try:
            matcher = build_matcher(str(name), param)
        except ValueError as problem:
            raise PolicyError(f"{where}.match: {problem}") from None
        unsupported = sorted(tools - matcher.tools)
        if unsupported:
            raise PolicyError(
                f"{where}.match: '{name}' cannot judge tool(s) {', '.join(unsupported)}"
            )
        matchers.append(matcher)
    return Rule(spec=spec, tools=tools, matchers=tuple(matchers))


def _spec(value: object, where: str, allowed: frozenset[str] | set[str] | None = None) -> RuleSpec:
    keys = {"id", "decision", "risk", "reason"}
    item = _mapping(value, where, required=keys, allowed=allowed or keys)
    rule_id, reason = item["id"], item["reason"]
    if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
        raise PolicyError(f"{where}.id: must look like 'group.name' (lowercase)")
    if not isinstance(reason, str) or not reason.strip():
        raise PolicyError(f"{where}.reason: must be a plain-English sentence")
    decision, risk = item["decision"], item["risk"]
    if decision not in {d.value for d in Decision} or not isinstance(decision, str):
        raise PolicyError(f"{where}.decision: must be allow, ask or deny")
    if risk not in {r.value for r in Risk} or not isinstance(risk, str):
        raise PolicyError(f"{where}.risk: must be low, medium, high or critical")
    return RuleSpec(rule_id, Decision(decision), Risk(risk), reason.strip())


# --- helpers -------------------------------------------------------------------------


def _mapping(
    value: object,
    where: str,
    *,
    required: frozenset[str] | set[str] = frozenset(),
    allowed: frozenset[str] | set[str] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PolicyError(f"{where}: must be a mapping")
    missing = sorted(set(required) - set(value))
    if missing:
        raise PolicyError(f"{where}: missing {', '.join(missing)}")
    permitted = allowed if allowed is not None else (required or None)
    if permitted is not None:
        extra = sorted(set(value) - set(permitted))
        if extra:
            raise PolicyError(f"{where}: unknown key(s) {', '.join(extra)}")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PolicyError(f"{where}: must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise PolicyError(f"{where}: every entry must be non-empty text")
    return tuple(item.strip() for item in value)


def _absolute_paths(value: object, where: str) -> tuple[str, ...]:
    paths = _strings(value, where)
    if not all(path.startswith("/") for path in paths):
        raise PolicyError(f"{where}: every entry must be an absolute path")
    return paths


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as problem:
        raise PolicyError(f"{where}: invalid regular expression ({problem})") from None


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses duplicate mapping keys instead of silently keeping one."""

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Hashable, object]:
        seen: set[object] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)
