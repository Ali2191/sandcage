"""The diff guard: new secrets, new network/exec code and weakened tests."""

from __future__ import annotations

import re

import pytest

from sandcage.policy.diff_guard import DiffGuardConfig, NetworkExecPattern, check_write, weakening
from tests.policy_helpers import engine
from tests.secrets_fixtures import GITHUB_TOKEN, STRIPE_KEY


def config() -> DiffGuardConfig:
    return engine().rulebook.diff_guard


def _test_globs() -> tuple[str, ...]:
    return engine().rulebook.context.test_files


def hits(path: str, old: str | None, new: str, *, may_edit_tests: bool = False) -> list[str]:
    found = check_write(config(), _test_globs(), path, old, new, may_edit_tests=may_edit_tests)
    return [hit.rule_id for hit in found]


# --- secrets -------------------------------------------------------------------------


def test_new_secret_is_denied() -> None:
    assert hits("app/config.py", "X = 1\n", f'KEY = "{STRIPE_KEY}"\n') == ["diff.deny_secrets"]


def test_secret_already_in_the_file_is_not_new() -> None:
    old = f'KEY = "{STRIPE_KEY}"\n'
    assert hits("app/config.py", old, old + "DEBUG = True\n") == []


def test_swapping_in_a_different_secret_is_new() -> None:
    old = f'KEY = "{STRIPE_KEY}"\n'
    new = f'KEY = "{GITHUB_TOKEN}"\n'
    assert hits("app/config.py", old, new) == ["diff.deny_secrets"]


def test_masked_placeholder_written_back_is_denied() -> None:
    old = "TOKEN = load()\n"
    new = 'TOKEN = "[MASKED:github_token]"\n'
    found = check_write(config(), _test_globs(), "app/config.py", old, new, may_edit_tests=False)
    assert [hit.rule_id for hit in found] == ["diff.deny_secrets"]
    assert "masked placeholder" in found[0].reason


def test_existing_placeholders_do_not_count() -> None:
    old = "# example: [MASKED:email]\n"
    assert hits("README.md", old, old + "More docs.\n") == []


@pytest.mark.parametrize(
    "line",
    [
        'api_key = os.environ["API_KEY"]\n',
        'token = os.getenv("GITHUB_TOKEN", "")\n',
        "password = settings.DB_PASSWORD\n",
        'password = "short"\n',
        'contact = "ops@example.com"\n',
    ],
)
def test_environment_lookups_short_values_and_emails_are_not_secrets(line: str) -> None:
    assert hits("app/config.py", "", line) == []


def test_literal_secret_named_assignment_is_a_secret() -> None:
    assert hits("app/config.py", "", 'db_password = "correct-horse-battery"\n') == [
        "diff.deny_secrets"
    ]


# --- network and exec code ------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\n",
        "import requests\n",
        "requests.post(url, data=x)\n",
        "from urllib.request import urlopen\n",
        "import socket\n",
        "import http.client\n",
        "os.system('ls')\n",
        "os.popen('ls')\n",
        "os.execv('/bin/sh', [])\n",
        "eval(text)\n",
        "exec(code)\n",
        "__import__('os')\n",
        "importlib.import_module(name)\n",
    ],
)
def test_new_network_or_exec_code_asks(code: str) -> None:
    assert hits("invoice_calc/invoice.py", "x = 1\n", "x = 1\n" + code) == ["diff.ask_network_exec"]


@pytest.mark.parametrize(
    "code",
    [
        "my_requests = 3\n",
        "model.eval()\n",
        "result = evaluate(x)\n",
        "websocket_url = 'x'\n",
        "socket_path = 'x'\n",
        "value = ast.literal_eval(text)\n",
        "cursor.execute(query)\n",
        "subprocesses = []\n",
    ],
)
def test_look_alike_names_are_not_flagged(code: str) -> None:
    assert hits("invoice_calc/invoice.py", "", code) == []


def test_only_new_occurrences_count() -> None:
    old = "import subprocess\n\nsubprocess.run(['pytest'])\n"
    rewritten = "import subprocess\n\n\nsubprocess.run(['pytest', '-q'])\n"
    assert hits("tools/run.py", old, rewritten) == []
    assert hits("tools/run.py", old, old + "subprocess.run(['ls'])\n") == ["diff.ask_network_exec"]


def test_only_code_files_are_scanned() -> None:
    assert hits("README.md", "", "Use subprocess and requests.\n") == []
    assert hits("scripts/setup.sh", "", "python -c 'import socket'\n") == ["diff.ask_network_exec"]


def test_reason_names_what_was_added() -> None:
    found = check_write(
        config(), _test_globs(), "a.py", "", "import subprocess, socket\n", may_edit_tests=False
    )
    assert "socket" in found[0].reason
    assert "subprocess" in found[0].reason


def test_patterns_come_from_the_policy_file() -> None:
    narrow = DiffGuardConfig(
        code_files=("*.py",),
        network_exec=(NetworkExecPattern("requests", re.compile(r"\brequests\b")),),
        test_config_files=(),
        secrets_rule=config().secrets_rule,
        network_exec_rule=config().network_exec_rule,
        weakened_tests_rule=config().weakened_tests_rule,
    )
    found = check_write(
        narrow, _test_globs(), "a.py", "", "import subprocess\n", may_edit_tests=False
    )
    assert found == []
    found = check_write(
        narrow, _test_globs(), "a.py", "", "import requests\n", may_edit_tests=False
    )
    assert [hit.rule_id for hit in found] == ["diff.ask_network_exec"]


# --- weakened tests ------------------------------------------------------------------

TESTS = """\
import pytest


def test_discount():
    assert apply_discount(200, 10) == 180


def test_zero():
    assert apply_discount(100, 0) == 100
    assert apply_discount(0, 50) == 0
"""


@pytest.mark.parametrize(
    ("new", "phrase"),
    [
        ("", "deletes or empties"),
        ("   \n", "deletes or empties"),
        (TESTS.split("def test_zero", maxsplit=1)[0], "removes tests: test_zero"),
        (
            TESTS.replace("    assert apply_discount(0, 50) == 0\n", ""),
            "removes assertions (3 to 2)",
        ),
        (TESTS.replace("def test_zero", "@pytest.mark.skip\ndef test_zero"), "skip"),
        (TESTS.replace("def test_zero", "@pytest.mark.xfail\ndef test_zero"), "skip"),
        (
            TESTS.replace(
                "    assert apply_discount(100", "    pytest.skip()\n    assert apply_discount(100"
            ),
            "skip",
        ),
    ],
)
def test_weakened_tests_are_denied(new: str, phrase: str) -> None:
    found = check_write(
        config(), _test_globs(), "tests/test_invoice.py", TESTS, new, may_edit_tests=False
    )
    assert [hit.rule_id for hit in found] == ["diff.deny_weakened_tests"]
    assert phrase in found[0].reason


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("pytest.ini", "[pytest]\n", "[pytest]\naddopts = --deselect tests/test_invoice.py\n"),
        ("pytest.ini", "[pytest]\n", "[pytest]\naddopts = -k 'not discount'\n"),
        ("setup.cfg", "[tool:pytest]\n", "[tool:pytest]\naddopts = --ignore=tests\n"),
        (
            "pyproject.toml",
            "[tool.pytest.ini_options]\n",
            '[tool.pytest.ini_options]\naddopts = "-p no:x --ignore-glob=*"\n',
        ),
        ("conftest.py", "", "collect_ignore = ['test_invoice.py']\n"),
        ("tox.ini", "", "[pytest]\nmarkers = skip_ci\n"),
    ],
)
def test_test_selection_added_to_config_is_denied(path: str, old: str, new: str) -> None:
    assert hits(path, old, new) == ["diff.deny_weakened_tests"]


def test_weakening_is_allowed_when_the_plan_allows_test_edits() -> None:
    assert hits("tests/test_invoice.py", TESTS, "", may_edit_tests=True) == []


def test_adding_tests_is_not_weakening() -> None:
    more = TESTS + "\n\ndef test_more():\n    assert apply_discount(50, 50) == 25\n"
    assert hits("tests/test_invoice.py", TESTS, more) == []
    assert hits("tests/test_new.py", None, more) == []


def test_unrelated_config_change_is_not_weakening() -> None:
    old = '[project]\nname = "x"\n'
    assert hits("pyproject.toml", old, old + 'version = "1.0"\n') == []


def test_weakening_handles_deleted_and_new_files() -> None:
    assert weakening("tests/test_a.py", TESTS, None, _test_globs(), ()) == [
        "It deletes or empties the test file."
    ]
    assert weakening("tests/test_a.py", None, TESTS, _test_globs(), ()) == []


def test_several_rules_can_match_one_write() -> None:
    new = f'import subprocess\nKEY = "{STRIPE_KEY}"\n'
    assert hits("tests/test_invoice.py", TESTS, new) == [
        "diff.deny_secrets",
        "diff.ask_network_exec",
        "diff.deny_weakened_tests",
    ]
