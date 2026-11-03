"""The shared glob helper, static path resolution and secret-file detection."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from sandcage.policy.globs import glob_match
from sandcage.policy.paths import Workspace
from tests.policy_helpers import engine

WS = Workspace(PurePosixPath("/workspace"))


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.pem", "server.pem", True),
        ("*.pem", "keys/deep/server.pem", True),
        ("tests", "tests/test_a.py", True),
        ("tests", "pkg/tests/test_a.py", True),
        ("tests", "contests/a.py", False),
        ("conftest.py", "a/b/conftest.py", True),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/x/a.py", False),
        ("src/*.py", "lib/src/a.py", False),
        ("invoice_calc/**", "invoice_calc/calc.py", True),
        ("invoice_calc/**", "invoice_calc", True),
        ("invoice_calc/", "invoice_calc/sub/calc.py", True),
        ("invoice_calc/**", "invoice_calculator/x.py", False),
        ("**/.git/config", ".git/config", True),
        ("**/.git/config", "vendor/lib/.git/config", True),
        ("**/.git/config", ".git/config.bak", False),
        ("a/**/z.py", "a/z.py", True),
        ("a/**/z.py", "a/b/c/z.py", True),
        ("", "a.py", False),
        ("*.py", "", False),
        ("./a.py", "a.py", True),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def test_glob_match_case() -> None:
    assert not glob_match(".env", ".ENV")
    assert glob_match(".env", ".ENV", case_sensitive=False)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a.py", "/workspace/a.py"),
        ("./a/../b.py", "/workspace/b.py"),
        ("/workspace/x", "/workspace/x"),
        ("..", "/"),
        ("//etc/passwd", "/etc/passwd"),
        (".", "/workspace"),
        ("~/x", None),
        ("~", None),
        ("$HOME/x", None),
        ("a/${X}", None),
        ("`id`", None),
        ("a\x00b", None),
        ("", None),
    ],
)
def test_resolve(path: str, expected: str | None) -> None:
    resolved = WS.resolve(path)
    assert (None if resolved is None else str(resolved)) == expected


def test_contains_and_relative() -> None:
    assert WS.contains(PurePosixPath("/workspace"))
    assert WS.contains(PurePosixPath("/workspace/a/b"))
    assert not WS.contains(PurePosixPath("/workspacex"))
    assert WS.relative(PurePosixPath("/workspace/a/b")) == "a/b"
    assert WS.label("./a/../README.md") == "README.md"
    assert WS.label("/etc/passwd") == "/etc/passwd"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/workspace/.env", True),
        ("/workspace/app/.Env.Local", True),
        ("/workspace/.envrc", True),
        ("/workspace/my_secrets.json", True),
        ("/workspace/client-credentials.txt", True),
        ("/workspace/access_token", True),
        ("/workspace/secretary.py", False),
        ("/workspace/environment.py", False),
        ("/workspace/keys.py", False),
        ("/home/user/.ssh/id_rsa", True),
        ("/workspace/.ssh/known_hosts", True),
    ],
)
def test_secret_files(path: str, expected: bool) -> None:
    ctx = engine().rulebook.context
    assert ctx.secrets.matches(ctx.workspace, PurePosixPath(path)) is expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (".env*", True),
        (".e*", True),
        ("*.pem", True),
        ("id_*", True),
        ("*", False),
        ("*.py", False),
    ],
)
def test_glob_arguments_that_may_expand_to_secrets(pattern: str, expected: bool) -> None:
    assert engine().rulebook.context.secrets.glob_may_match(pattern) is expected
