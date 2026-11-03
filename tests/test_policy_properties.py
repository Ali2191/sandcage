"""The gate judges model output, so no input may crash it or slip past as an exception."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from sandcage.policy import Decision
from sandcage.policy.commands import parse_command
from tests.policy_helpers import fetch, read, run, session, write

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=10), children, max_size=4)
    ),
    max_leaves=12,
)
_TOOLS = st.sampled_from(
    ["read_file", "write_file", "list_dir", "run_command", "http_get", "rollback", "finish", "x"]
)
_ACTIONS = st.one_of(
    _JSON,
    st.fixed_dictionaries({"tool": _TOOLS, "args": _JSON}),
    st.fixed_dictionaries(
        {
            "tool": _TOOLS,
            "args": st.dictionaries(
                st.sampled_from(["path", "command", "url", "content", "checkpoint"]),
                _JSON,
                max_size=3,
            ),
        }
    ),
)
_SHELLISH = st.one_of(
    st.text(alphabet="abc .=/~$`'\"\\|&;()<>{}#*\n-ecurlshm", max_size=60),
    st.lists(
        st.sampled_from(["curl", "sudo", "sh", "-c", "'", '"', "$(", ")", "|", ";", "env", "x"]),
        max_size=8,
    ).map(" ".join),
)
NETWORK_TOOLS = frozenset({"curl", "wget", "nc", "ssh", "scp"})


@given(st.lists(_ACTIONS, max_size=6))
def test_session_never_raises_on_arbitrary_actions(actions: list[object]) -> None:
    s = session({"README.md": "hi"})
    for action in actions:
        decision = s.evaluate(action)
        assert decision.decision in set(Decision)
        assert decision.rule_id
        assert decision.reason


@given(_SHELLISH, st.booleans())
def test_any_command_gets_a_decision(command: str, tainted: bool) -> None:
    s = session({"README.md": "hi"})
    if tainted:
        s.evaluate(read("README.md"))
    decision = s.evaluate(run(command))
    if parse_command(command).programs & NETWORK_TOOLS:
        assert decision.decision is not Decision.ALLOW


@given(st.text(max_size=60), st.text(max_size=60))
def test_any_path_or_url_gets_a_decision(path: str, url: str) -> None:
    s = session()
    assert s.evaluate(read(path)).decision in set(Decision)
    assert s.evaluate(write(path, path)).decision in set(Decision)
    assert s.evaluate(fetch(url)).decision in set(Decision)
