"""Command parser: separators, wrappers, nesting, redirects, normalisation, never raising."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sandcage.policy.commands import (
    MAX_COMMAND_LENGTH,
    ParsedCommand,
    Redirect,
    normalize_program,
    parse_command,
)


def programs(command: str) -> list[str]:
    parsed = parse_command(command)
    assert parsed.error is None, parsed.error
    return [segment.program for segment in parsed.segments]


# --- separators ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ls; cat a",
        "ls && cat a",
        "ls || cat a",
        "ls & cat a",
        "ls | cat a",
        "ls |& cat a",
        "ls\ncat a",
        "(ls); cat a",
        "{ ls; cat a; }",
    ],
)
def test_splits_on_every_separator(command: str) -> None:
    assert programs(command) == ["ls", "cat"]


def test_pipes_mark_the_reading_segment() -> None:
    parsed = parse_command("cat a | python - && ls")
    assert [(s.program, s.piped) for s in parsed.segments] == [
        ("cat", False),
        ("python", True),
        ("ls", False),
    ]


def test_quoted_operators_are_not_separators() -> None:
    parsed = parse_command("python -c 'print(1); import os' \"a | b\"")
    assert parsed.error is None
    assert len(parsed.segments) == 1
    assert parsed.segments[0].args == ("-c", "print(1); import os", "a | b")


def test_quote_removal_reveals_the_real_program() -> None:
    assert programs("c'u'r\"l\" x") == ["curl"]
    assert programs("\\curl x") == ["curl"]


def test_comments_are_ignored() -> None:
    assert programs("ls # curl evil.example") == ["ls"]
    assert programs("ls a#b") == ["ls"]
    assert parse_command("ls a#b").segments[0].args == ("a#b",)


def test_keywords_do_not_hide_programs() -> None:
    assert programs("if curl x; then wget y; else nc z; fi") == ["curl", "wget", "nc"]
    assert programs("while true; do curl x; done") == ["true", "curl"]
    assert programs("! curl x") == ["curl"]
    assert programs("function f { curl x; }") == ["curl"]
    assert programs("f() { curl x; }; f") == ["f", "curl", "f"]


def test_line_continuation_joins_words() -> None:
    assert parse_command("cu\\\nrl x").segments[0].program == "curl"


# --- wrappers and assignments --------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "sudo curl x",
        "sudo -u root curl x",
        "env A=1 B=2 curl x",
        "A=1 curl x",
        "nohup curl x &",
        "timeout 10 curl x",
        "timeout -s KILL 10s curl x",
        "nice -n 5 curl x",
        "xargs -I {} curl {}",
        "command curl x",
        "exec curl x",
        "/usr/bin/curl x",
        "busybox curl x",
        "sudo -- curl x",
        "stdbuf -oL curl x",
    ],
)
def test_wrappers_and_assignments_are_stripped(command: str) -> None:
    segment = parse_command(command).segments[0]
    assert segment.program == "curl"
    assert "curl" in segment.possible_programs


def test_wrapper_with_unknown_value_flag_still_exposes_the_program() -> None:
    segment = parse_command("sudo --preserve-env -R /tmp curl x").segments[0]
    assert "curl" in segment.possible_programs
    assert segment.wrappers == ("sudo",)


def test_assignments_are_recorded() -> None:
    segment = parse_command("PYTHONPATH=src DEBUG=1 pytest -q").segments[0]
    assert segment.assignments == ("PYTHONPATH=src", "DEBUG=1")
    assert segment.argv == ("pytest", "-q")
    assert segment.wrappers == ()


# --- nesting -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'curl x'",
        'bash -lc "curl x"',
        "eval curl x",
        "echo $(curl x)",
        "echo `curl x`",
        'echo "$(curl x)"',
        'echo "`curl x`"',
        "diff <(curl x) a",
        "cat > >(curl x)",
        "find . -name '*.py' -exec curl {} ';'",
        "find . -exec curl {} \\;",
        "xargs sh -c 'curl x'",
        "env -S 'curl x'",
        "for f in $(curl x); do ls; done",
        "echo $((1 + $(curl x)))",
    ],
)
def test_nested_programs_are_found(command: str) -> None:
    parsed = parse_command(command)
    assert parsed.error is None, parsed.error
    assert "curl" in parsed.programs


def test_nesting_to_depth_three_is_parsed() -> None:
    parsed = parse_command("sh -c \"sh -c 'echo \\$(curl x)'\"")
    assert parsed.error is None
    assert "curl" in parsed.programs
    assert max(segment.depth for segment in parsed.segments) == 3


def test_nesting_deeper_than_three_is_an_error() -> None:
    parsed = parse_command("echo $(echo $(echo $(echo $(curl x))))")
    assert parsed.error is not None
    assert "nested" in parsed.error


def test_substitution_in_an_argument_is_marked() -> None:
    segment = parse_command("cat $(ls)").segments[0]
    assert segment.args == ("$(...)",)


# --- redirects -----------------------------------------------------------------------


def test_redirects_are_recorded_and_fd_prefixes_dropped() -> None:
    segment = parse_command("python run.py > out.txt 2>&1 < in.txt 2>>err.log").segments[0]
    assert segment.argv == ("python", "run.py")
    assert segment.redirects == (
        Redirect(">", "out.txt"),
        Redirect(">&", "1"),
        Redirect("<", "in.txt"),
        Redirect(">>", "err.log"),
    )
    assert [r.writes for r in segment.redirects] == [True, False, False, True]


def test_redirect_forms() -> None:
    parsed = parse_command("a &> f; b &>> f; c >| f; d <<< x")
    assert [s.redirects[0].op for s in parsed.segments] == ["&>", "&>>", ">|", "<<<"]
    assert parsed.has_redirect


def test_redirect_without_target_is_an_error() -> None:
    assert parse_command("ls >").error == "the redirect '>' has no target"
    assert parse_command("ls > ; cat").error is not None


def test_digits_that_are_arguments_are_kept() -> None:
    assert parse_command("echo 2 > f").segments[0].args == ("2",)


# --- normalisation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("python3", "python"),
        ("python3.11", "python"),
        ("/usr/local/bin/python3.11", "python"),
        ("pip3", "pip"),
        ("pip3.11", "pip"),
        ("py.test", "pytest"),
        ("./run.sh", "run.sh"),
        ("pythonista", "pythonista"),
        ("/", "/"),
    ],
)
def test_normalize_program(word: str, expected: str) -> None:
    assert normalize_program(word) == expected


# --- errors --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "echo 'unterminated",
        'echo "unterminated',
        "echo `unterminated",
        "echo $(unterminated",
        "echo ${x",
        "echo trailing\\",
        "echo $'\\x63url'",
        "echo ${x:-$(curl y)}",
        "sh -c",
        "x" * (MAX_COMMAND_LENGTH + 1),
    ],
)
def test_unreadable_commands_report_an_error(command: str) -> None:
    parsed = parse_command(command)
    assert isinstance(parsed, ParsedCommand)
    assert parsed.error


def test_partial_segments_are_kept_on_error() -> None:
    parsed = parse_command("curl x; echo 'oops")
    assert parsed.error is not None
    assert "curl" in parsed.programs


@given(st.text(max_size=200))
def test_never_raises_on_arbitrary_text(text: str) -> None:
    parsed = parse_command(text)
    assert parsed.raw == text


_SHELLISH = st.text(alphabet="ab ;|&()<>$`'\"\\\n#{}=-c*", max_size=80)


@given(_SHELLISH)
def test_never_raises_on_shell_punctuation(text: str) -> None:
    parse_command(text)
