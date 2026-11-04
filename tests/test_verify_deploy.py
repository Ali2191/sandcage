"""scripts/verify_deploy.py: its judgements on their own, and the whole checklist against a fake
edge that behaves well, then against one fake edge per specific bad behaviour (each must be
caught: a verifier that cannot fail proves nothing)."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.fake_edge import GOOD_HEADERS, edge

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_deploy.py"
CODE = "Verifier-Test-Code-31337"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_deploy", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_deploy"] = module
    spec.loader.exec_module(module)
    return module


vd = load()


# --- judgements --------------------------------------------------------------------------------


def good_https_headers() -> dict[str, str]:
    return {**GOOD_HEADERS, "Strict-Transport-Security": "max-age=31536000; includeSubDomains"}


def test_complete_headers_pass() -> None:
    assert vd.judge_headers(good_https_headers(), over_tls=True) == []


@pytest.mark.parametrize(
    "missing",
    [
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
        "Permissions-Policy",
    ],
)
def test_each_missing_security_header_is_named(missing: str) -> None:
    headers = good_https_headers()
    del headers[missing]
    problems = vd.judge_headers(headers, over_tls=True)
    assert any(missing.lower() in problem.lower() for problem in problems), problems


def test_hsts_is_not_expected_over_plain_http() -> None:
    headers = good_https_headers()
    del headers["Strict-Transport-Security"]
    assert vd.judge_headers(headers, over_tls=False) == []


def test_a_server_header_and_a_weak_csp_are_named() -> None:
    headers = {**good_https_headers(), "Server": "Caddy"}
    assert any("Server header" in p for p in vd.judge_headers(headers, over_tls=True))
    weak = {**good_https_headers(), "Content-Security-Policy": "default-src * 'unsafe-eval'"}
    problems = vd.judge_headers(weak, over_tls=True)
    assert any("default-src 'self'" in p for p in problems)
    assert any("unsafe-eval" in p for p in problems)


def test_cors_judgement() -> None:
    evil = "https://evil.example"
    assert vd.judge_cors({}, evil) is None
    assert vd.judge_cors({"Access-Control-Allow-Origin": "*"}, evil)
    assert vd.judge_cors({"access-control-allow-origin": evil}, evil)
    assert vd.judge_cors({"Access-Control-Allow-Origin": "https://panel.example"}, evil) is None


def test_leaks_are_found_and_described_without_the_secret() -> None:
    clean = ['200\nserver: x\n{"ok": true}']
    assert vd.find_leaks(clean, CODE) == []
    found = vd.find_leaks(["200\n\n" + CODE], CODE)
    assert found == ["response #1 contains the access code"]
    assert CODE not in "".join(found)
    shaped = vd.find_leaks(['200\n\n{"api_key": "abcdEFGH12345678"}'], None)
    assert shaped
    assert vd.find_leaks(["200\n\nsk-" + "Ab1" * 8], None)
    assert vd.find_leaks(["200\n\nAuthorization: Bearer abcdefghijklmnop12345"], None)


def test_event_stream_lines_are_parsed() -> None:
    pending: dict[str, str] = {}
    assert vd.parse_sse_line("id: 7\n", pending) is None
    assert vd.parse_sse_line("event: step\n", pending) is None
    assert vd.parse_sse_line('data: {"a": 1}\n', pending) is None
    event = vd.parse_sse_line("\n", pending)
    assert (event.id, event.kind, event.data) == (7, "step", '{"a": 1}')
    assert pending == {}
    assert vd.parse_sse_line(": ping\n", pending) == "ping"
    assert vd.parse_sse_line("\n", pending) is None


def test_the_target_is_read_from_the_url() -> None:
    target = vd.target_from("https://203-0-113-7.sslip.io", rehearsal=False)
    assert (target.host, target.port, target.tls) == ("203-0-113-7.sslip.io", 443, True)
    local = vd.target_from("http://127.0.0.1:8123", rehearsal=True)
    assert (local.port, local.tls, local.origin) == (8123, False, "http://127.0.0.1:8123")
    for bad in ("ftp://x", "not a url", "https://"):
        with pytest.raises(SystemExit):
            vd.target_from(bad, rehearsal=False)


def test_the_code_file_must_be_private_and_is_never_echoed(tmp_path: Path) -> None:
    path = tmp_path / "code"
    path.write_text(CODE + "\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(SystemExit) as loose:
        vd.read_code(path)
    assert CODE not in str(loose.value)
    assert "600" in str(loose.value)
    path.chmod(0o600)
    assert vd.read_code(path) == CODE
    path.write_text("two words\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        vd.read_code(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(SystemExit):
        vd.read_code(link)


# --- the whole checklist against a fake edge ---------------------------------------------------


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vd, "TIMEOUT", 3.0)
    monkeypatch.setattr(vd, "FIRST_EVENT_SECONDS", 1.5)
    monkeypatch.setattr(vd, "HEARTBEAT_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(vd, "RESUME_WAIT_SECONDS", 4.0)
    free = []
    for _ in range(3):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free.append(probe.getsockname()[1])
    monkeypatch.setattr(vd, "CLOSED_PORTS", tuple(free))
    monkeypatch.setattr(vd, "SSH_PORT", free[0])


def verify(url: str, tmp_path: Path, *, with_code: bool = True) -> tuple[int, dict[str, str], str]:
    args = [url, "--rehearsal"]
    if with_code:
        path = tmp_path / "code"
        path.write_text(CODE + "\n", encoding="utf-8")
        path.chmod(0o600)
        args += ["--code-file", str(path)]
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = vd.main(args)
    rows: dict[str, str] = {}
    for line in out.getvalue().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in {"PASS", "FAIL", "WARN", "UNTESTED"}:
            rows[parts[1].split("  ")[0].strip()] = parts[0]
    return status, rows, out.getvalue() + err.getvalue()


def test_a_healthy_edge_passes_and_names_what_it_could_not_test(tmp_path: Path) -> None:
    with edge(CODE) as (url, state):
        status, rows, output = verify(url, tmp_path)
    assert status == 0, output
    assert rows["healthz"] == "PASS"
    assert rows["readyz"] == "PASS"
    assert rows["sandbox runtime stated"] == "PASS"
    assert rows["changing call refused without the code"] == "PASS"
    assert rows["changing call refused with a wrong code"] == "PASS"
    assert rows["right code accepted"] == "PASS"
    assert rows["event stream streams"] == "PASS"
    assert rows["stream heartbeat"] == "PASS"
    assert rows["Last-Event-ID resume"] == "PASS"
    assert rows["oversized body refused unread"] == "PASS"
    assert rows["CORS allows no other origin"] == "PASS"
    assert rows["no secret in any response"] == "PASS"
    # What needs a real VM or a switch the script cannot flip is said, not skipped silently.
    assert rows["https certificate"] == "UNTESTED"
    assert rows["kill switch honoured"] == "UNTESTED"
    assert state.started == 1
    assert state.stopped.is_set(), "the scripted run is stopped again"
    assert CODE not in output, "the code is never printed"


def test_without_a_code_file_the_stream_checks_are_untested_not_failed(tmp_path: Path) -> None:
    with edge(CODE) as (url, state):
        status, rows, output = verify(url, tmp_path, with_code=False)
    assert status == 0, output
    assert rows["event stream streams"] == "UNTESTED"
    assert rows["Last-Event-ID resume"] == "UNTESTED"
    assert rows["right code accepted"] == "UNTESTED"
    assert state.started == 0, "no run is started without the code"


def test_plain_docker_is_a_loud_warning_not_a_failure(tmp_path: Path) -> None:
    with edge(CODE, "runc") as (url, _):
        status, rows, output = verify(url, tmp_path)
    assert status == 0
    assert rows["sandbox runtime stated"] == "WARN"
    assert "gVisor is NOT in use" in output
    assert "NOT running sandboxes in gVisor" in output


def test_a_paused_demo_is_checked_against_the_kill_switch(tmp_path: Path) -> None:
    with edge(CODE, "paused") as (url, state):
        status, rows, output = verify(url, tmp_path)
    assert rows["kill switch honoured"] == "PASS", output
    # A paused demo cannot start the scripted run: the stream checks are untested, not failed.
    assert rows["event stream streams"] == "UNTESTED"
    assert rows["Last-Event-ID resume"] == "UNTESTED"
    assert status == 0, output
    assert state.started == 0


@pytest.mark.parametrize(
    ("bad", "failing"),
    [
        ("not_ready", "readyz"),
        ("no_runtime", "sandbox runtime stated"),
        ("accepts_no_code", "changing call refused without the code"),
        ("buffered_sse", "event stream streams"),
        ("no_heartbeat", "stream heartbeat"),
        ("replay_on_resume", "Last-Event-ID resume"),
        ("no_413", "oversized body refused unread"),
        ("cors_star", "CORS allows no other origin"),
        ("cors_preflight_open", "CORS allows no other origin"),
        ("server_header", "security headers on the panel"),
        ("no_csp", "security headers on the panel"),
        ("compressed_api", "API is not compressed"),
        ("leak_code", "no secret in any response"),
        ("leak_key", "no secret in any response"),
    ],
)
def test_each_bad_behaviour_is_caught(tmp_path: Path, bad: str, failing: str) -> None:
    with edge(CODE, bad) as (url, _):
        status, rows, output = verify(url, tmp_path)
    assert status == 1, f"{bad} was not caught:\n{output}"
    assert rows[failing] == "FAIL", f"{bad}: expected {failing} to fail\n{output}"
    assert CODE not in output


def test_an_open_dangerous_port_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as listener, edge(CODE) as (url, _):
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(vd, "CLOSED_PORTS", (port,))
        status, rows, output = verify(url, tmp_path)
    assert status == 1, output
    assert rows["no Docker, API, panel or admin port reachable"] == "FAIL"


def test_an_unreachable_server_fails_instead_of_passing(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    status, rows, output = verify(f"http://127.0.0.1:{port}", tmp_path)
    assert status == 1, output
    assert rows["healthz"] == "FAIL"
    assert rows["readyz"] == "FAIL"
