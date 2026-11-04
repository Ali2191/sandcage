"""deploy/deploy.sh against a fake host: idempotent re-runs, an atomic mode-600 env file, no secret
ever echoed or put on a command line, a short access code refused in public mode, a truncated
download that does nothing, a script that survives updating itself, gVisor never claimed
falsely, and a destroy path that asks first. The script is the real file; the host is fake
(tests/deploy_host.py)."""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.deploy_host import (
    ACCESS_CODE,
    LLM_KEY,
    SCRIPT,
    FakeHost,
    Result,
)

EMAIL = "ops@example.com"


@pytest.fixture
def host(tmp_path: Path) -> FakeHost:
    return FakeHost(tmp_path)


def up(host: FakeHost, *extra: str, env: dict[str, str] | None = None) -> Result:
    """A normal first deploy: code and key from mode-600 files, no checkout updates."""
    return host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(host.key_file()),
        *extra,
        env=env,
    )


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- the first deploy ---------------------------------------------------------------------------


def test_a_first_deploy_writes_private_files_builds_the_image_and_starts_the_stack(
    host: FakeHost,
) -> None:
    done = up(host)
    assert done.code == 0, done.output
    assert mode_of(host.env_file) == 0o600
    assert mode_of(host.etc / "llm-api-key") == 0o600
    assert mode_of(host.etc) == 0o700
    assert mode_of(host.state / "work") == 0o700
    assert mode_of(host.state / "data") == 0o700
    values = host.env_values()
    assert values["SITE_ADDRESS"] == "203-0-113-7.sslip.io"
    assert values["ACME_EMAIL"] == EMAIL
    assert values["DEMO_ACCESS_CODE"] == ACCESS_CODE
    assert values["LLM_PROVIDER"] == "vultr"
    assert values["LLM_MODEL"] == "deepseek-v4.1-flash-1017"
    assert values["SANDBOX_RUNTIME"] == "runsc"
    assert values["SANDCAGE_WORK_DIR"] == str(host.state / "work")
    assert values["SANDCAGE_DATA_DIR"] == str(host.state / "data")
    assert values["SANDCAGE_LLM_KEY_FILE"] == str(host.etc / "llm-api-key")
    assert (host.etc / "llm-api-key").read_text(encoding="utf-8").strip() == LLM_KEY
    # The key file and both directories belong to the API's user (uid 10001), nobody else.
    chowns = host.calls_matching("chown 10001:10001")
    assert len(chowns) == 3
    # The image is built on the host, then gVisor is probed, then the stack starts.
    order = [
        c for c in host.calls() if c.startswith(("docker build", "docker run", "docker compose"))
    ]
    kinds = [c.split()[1] for c in order]
    assert (
        kinds.index("build")
        < kinds.index("run")
        < max(i for i, k in enumerate(kinds) if k == "compose")
    )
    start = host.calls_matching(" up -d --build --remove-orphans")
    assert len(start) == 1
    assert f"--env-file {host.env_file}" in start[0]
    assert "-p sandcage" in start[0]
    assert f"https://{values['SITE_ADDRESS']}/" in done.stdout
    assert "gVisor (runsc)" in done.stdout


def test_the_summary_and_every_call_contain_no_secret(host: FakeHost) -> None:
    done = up(host)
    assert done.code == 0, done.output
    everything = done.output + "\n".join(host.calls())
    assert ACCESS_CODE not in everything
    assert LLM_KEY not in everything
    # No leftover temp file holds a secret either.
    assert not list(host.etc.glob(".tmp.*"))


def test_the_site_falls_back_to_the_public_address_when_the_route_is_private(
    host: FakeHost,
) -> None:
    done = up(host, env={"FAKE_IP_SRC": "10.1.2.3", "FAKE_PUBLIC_IP": "198.51.100.9"})
    assert done.code == 0, done.output
    assert host.env_values()["SITE_ADDRESS"] == "198-51-100-9.sslip.io"
    assert host.calls_matching("api.ipify.org")


def test_an_explicit_site_wins(host: FakeHost) -> None:
    done = up(host, "--site", "demo.example.org")
    assert done.code == 0, done.output
    assert host.env_values()["SITE_ADDRESS"] == "demo.example.org"


@pytest.mark.parametrize("site", ["bad host", "UPPER_case", "a..b", ".x", "x/y", "a;b"])
def test_a_bad_site_name_is_refused(host: FakeHost, site: str) -> None:
    done = up(host, "--site", site)
    assert done.code != 0
    assert not host.env_file.exists()


@pytest.mark.parametrize("email", ["nobody", "a@b", "a@@b.co", "x y@b.co", "@b.co", "a@b.co;rm"])
def test_a_bad_email_is_refused(host: FakeHost, email: str) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        email,
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(host.key_file()),
    )
    assert done.code != 0
    assert not host.env_file.exists()


def test_the_email_is_needed_the_first_time(host: FakeHost) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(host.key_file()),
    )
    assert done.code != 0
    assert "--email" in done.stderr
    assert not host.env_file.exists()


# --- idempotence --------------------------------------------------------------------------------


def test_running_it_twice_changes_nothing(host: FakeHost) -> None:
    assert up(host).code == 0
    first = host.snapshot()
    before = host.env_file.stat().st_mtime_ns
    key_before = (host.etc / "llm-api-key").stat().st_mtime_ns
    again = up(host)
    assert again.code == 0, again.output
    assert host.snapshot() == first, "the second run changed a file, a mode or a directory"
    assert host.env_file.stat().st_mtime_ns == before, "the env file was rewritten"
    assert (host.etc / "llm-api-key").stat().st_mtime_ns == key_before
    assert "unchanged" in again.stdout


def test_a_second_run_needs_no_secret_arguments(host: FakeHost) -> None:
    assert up(host).code == 0
    first = host.snapshot()
    again = host.run("--up", "--no-pull")
    assert again.code == 0, again.output
    assert host.snapshot() == first
    assert host.env_values()["DEMO_ACCESS_CODE"] == ACCESS_CODE


def test_values_an_operator_edited_survive_a_rerun_and_the_code_is_kept(host: FakeHost) -> None:
    assert up(host).code == 0
    text = host.env_file.read_text(encoding="utf-8").replace(
        "DEMO_RUNS_PER_DAY=40", "DEMO_RUNS_PER_DAY=5"
    )
    host.env_file.write_text(text, encoding="utf-8")
    other = host.code_file("A-Different-Code-9999")
    again = host.run("--up", "--no-pull", "--access-code-file", str(other))
    assert again.code == 0, again.output
    values = host.env_values()
    assert values["DEMO_RUNS_PER_DAY"] == "5"
    assert values["DEMO_ACCESS_CODE"] == ACCESS_CODE, "a rerun must not replace the code"


def test_rotating_the_code_replaces_it_only_when_asked(host: FakeHost) -> None:
    assert up(host).code == 0
    fresh = host.code_file("Rotated-Code-777777")
    again = host.run("--up", "--no-pull", "--rotate-code", "--access-code-file", str(fresh))
    assert again.code == 0, again.output
    assert host.env_values()["DEMO_ACCESS_CODE"] == "Rotated-Code-777777"
    assert "Rotated-Code-777777" not in again.output


# --- the access code ----------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["short", "elevenchars", "has a space 123", "quote'quote12345"])
def test_a_short_or_unusable_access_code_is_refused_in_public_mode(
    host: FakeHost, code: str
) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file(code)),
        "--llm-key-file",
        str(host.key_file()),
    )
    assert done.code != 0
    assert "access code must be" in done.stderr
    assert code not in done.output, "a refused code is never echoed"
    assert not host.env_file.exists()
    assert not host.calls_matching(" up -d")


def test_exactly_twelve_characters_are_accepted(host: FakeHost) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file("abcdefghijkl")),
        "--llm-key-file",
        str(host.key_file()),
    )
    assert done.code == 0, done.output


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o640, 0o666])
def test_a_secret_file_that_others_can_read_is_refused(host: FakeHost, mode: int) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.private_file("loose-code", ACCESS_CODE, mode)),
        "--llm-key-file",
        str(host.key_file()),
    )
    assert done.code != 0
    assert "mode" in done.stderr
    assert ACCESS_CODE not in done.output
    assert not host.env_file.exists()


def test_a_secret_file_owned_by_someone_else_is_refused(host: FakeHost) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(host.key_file()),
        env={"SUDO_UID": "424242"},
    )
    assert done.code != 0
    assert "owned by another user" in done.stderr
    assert not host.env_file.exists()


def test_a_key_file_that_is_a_link_is_refused(host: FakeHost) -> None:
    link = host.secrets / "key-link"
    link.symlink_to(host.key_file())
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(link),
    )
    assert done.code != 0
    assert "link" in done.stderr
    assert LLM_KEY not in done.output


@pytest.mark.parametrize("key", ["", "short", "has space in it 12345", 'quote"d-key-123456'])
def test_an_unusable_key_is_refused_without_echoing_it(host: FakeHost, key: str) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(host.private_file("bad-key", key)),
    )
    assert done.code != 0
    assert not host.env_file.exists()
    if key:
        assert key not in done.output


def test_a_hidden_prompt_takes_the_code_and_the_key_and_never_echoes_them(
    host: FakeHost,
) -> None:
    done = host.run_in_terminal("--up", "--no-pull", "--email", EMAIL, typed=[ACCESS_CODE, LLM_KEY])
    assert done.code == 0, done.output
    assert "Access code for the demo" in done.stdout
    assert "Vultr inference API key" in done.stdout
    assert ACCESS_CODE not in done.stdout, "the typed code was echoed to the terminal"
    assert LLM_KEY not in done.stdout, "the typed key was echoed to the terminal"
    assert host.env_values()["DEMO_ACCESS_CODE"] == ACCESS_CODE
    assert (host.etc / "llm-api-key").read_text(encoding="utf-8").strip() == LLM_KEY


def test_a_generated_code_is_shown_once_on_the_terminal_and_kept_private(
    host: FakeHost,
) -> None:
    done = host.run_in_terminal(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--generate-code",
        "--llm-key-file",
        str(host.key_file()),
        typed=[],
    )
    assert done.code == 0, done.output
    code = host.env_values()["DEMO_ACCESS_CODE"]
    assert re.fullmatch(r"[A-Za-z0-9]{20}", code)
    assert done.stdout.count(code) == 1, "shown exactly once"
    assert code not in "\n".join(host.calls())
    assert mode_of(host.env_file) == 0o600
    # A re-run never shows it again.
    again = host.run_in_terminal("--up", "--no-pull", "--generate-code", typed=[])
    assert again.code == 0, again.output
    assert code not in again.stdout


def test_a_generated_code_needs_a_terminal_so_it_is_never_written_to_a_log(
    host: FakeHost,
) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--generate-code",
        "--llm-key-file",
        str(host.key_file()),
    )
    assert done.code != 0
    assert "terminal" in done.stderr
    assert not host.env_file.exists()


def test_with_no_way_to_get_a_secret_the_script_stops_instead_of_guessing(
    host: FakeHost,
) -> None:
    done = host.run("--up", "--no-pull", "--email", EMAIL)
    assert done.code != 0
    assert "no access code" in done.stderr
    assert not host.env_file.exists()


# --- the env file is atomic and never empty -----------------------------------------------------


def test_a_failure_while_writing_leaves_no_empty_or_partial_env_file(host: FakeHost) -> None:
    # An mv that fails stands in for a power cut at the worst moment.
    mover = host.bin / "mv"
    mover.write_text('#!/bin/bash\necho "mv $*" >> "$FAKE_LOG"\nexit 1\n', encoding="utf-8")
    mover.chmod(0o755)
    done = up(host)
    assert done.code != 0
    assert not host.env_file.exists(), "no env file at all is acceptable; an empty one is not"
    assert not list(host.etc.glob(".tmp.*")), "the temporary copy must be cleaned up"
    assert not host.calls_matching(" up -d")


def test_a_failure_while_rewriting_keeps_the_previous_env_file_whole(host: FakeHost) -> None:
    assert up(host).code == 0
    before = host.env_file.read_text(encoding="utf-8")
    mover = host.bin / "mv"
    mover.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    mover.chmod(0o755)
    done = host.run("--up", "--no-pull", "--model", "glm-5.3")
    assert done.code != 0
    assert host.env_file.read_text(encoding="utf-8") == before


# --- prerequisites ------------------------------------------------------------------------------


def test_not_being_root_stops_before_anything_happens(host: FakeHost) -> None:
    done = up(host, env={"FAKE_UID": "1000"})
    assert done.code != 0
    assert "root" in done.stderr
    assert not host.calls()
    assert not host.etc.exists()


def test_a_missing_docker_is_named_with_the_runbook(host: FakeHost) -> None:
    (host.bin / "docker").unlink()
    done = host.run(
        "--up", "--no-pull", "--email", EMAIL, env={"PATH": f"{host.bin}:/usr/bin:/bin"}
    )
    assert done.code != 0
    assert "docker is not installed" in done.stderr
    assert "RUNBOOK" in done.stderr


def test_a_missing_compose_plugin_is_named(host: FakeHost) -> None:
    done = up(host, env={"FAKE_NO_COMPOSE": "1"})
    assert done.code != 0
    assert "compose plugin" in done.stderr
    assert not host.env_file.exists()


def test_a_docker_daemon_that_does_not_answer_is_named(host: FakeHost) -> None:
    done = up(host, env={"FAKE_DOCKER_DOWN": "1"})
    assert done.code != 0
    assert "daemon" in done.stderr


def test_too_little_disk_is_refused(host: FakeHost) -> None:
    done = up(host, env={"FAKE_FREE_KB": "1000000"})
    assert done.code != 0
    assert "10 GB" in done.stderr
    assert not host.env_file.exists()


def test_another_os_only_warns(host: FakeHost) -> None:
    host.os_release.write_text('ID=debian\nVERSION_ID="12"\n', encoding="utf-8")
    done = up(host)
    assert done.code == 0, done.output
    assert "tested on Ubuntu 24.04" in done.stderr


# --- gVisor is never claimed falsely ------------------------------------------------------------


def test_gvisor_is_probed_for_its_banner_and_recorded_when_seen(host: FakeHost) -> None:
    done = up(host)
    assert done.code == 0, done.output
    probes = host.calls_matching("--runtime=runsc")
    assert len(probes) == 1
    assert "--network none" in probes[0]
    assert "gVisor verified" in done.stdout
    assert host.env_values()["SANDBOX_RUNTIME"] == "runsc"


def test_without_the_banner_gvisor_is_refused_and_nothing_is_started(host: FakeHost) -> None:
    done = up(host, env={"FAKE_GVISOR": "0"})
    assert done.code != 0
    assert "gVisor (runsc) could not be verified" in done.stderr
    assert "--allow-runc-fallback" in done.stderr
    assert not host.env_file.exists()
    assert not host.calls_matching(" up -d")


def test_the_runc_fallback_is_explicit_loud_and_recorded_as_runc(host: FakeHost) -> None:
    done = up(
        host,
        "--allow-runc-fallback",
        env={
            "FAKE_GVISOR": "0",
            "FAKE_READY_JSON": (
                '{"ready": true, "checks": {"runtime": {"ok": true, "detail": "x",'
                ' "active": "runc", "label": "runc (plain Docker)", "gvisor": false}}}'
            ),
        },
    )
    assert done.code == 0, done.output
    assert host.env_values()["SANDBOX_RUNTIME"] == "runc"
    assert "falling back to plain Docker" in done.stderr
    assert "NOT be inside gVisor" in done.stderr
    assert "gVisor is NOT in use" in done.stderr
    assert "gVisor verified" not in done.stdout
    assert "runc (plain Docker)" in done.stdout


def test_asking_for_runc_skips_the_probe_and_still_warns(host: FakeHost) -> None:
    done = up(host, "--runtime", "runc")
    assert done.code == 0, done.output
    assert not host.calls_matching("--runtime=runsc")
    assert "NOT inside gVisor" in done.stderr
    assert host.env_values()["SANDBOX_RUNTIME"] == "runc"


def test_a_banner_cannot_be_faked_by_a_runtime_that_only_claims_the_name(host: FakeHost) -> None:
    # A docker that runs the container fine but prints no banner is not gVisor.
    docker = host.bin / "docker"
    docker.write_text(
        docker.read_text(encoding="utf-8").replace(
            'echo "[    0.000000] Starting gVisor..."; exit 0',
            'echo "Linux version 6.8 (a normal kernel)"; exit 0',
        ),
        encoding="utf-8",
    )
    done = up(host)
    assert done.code != 0
    assert "could not be verified" in done.stderr


def test_installing_gvisor_uses_the_official_repository_and_registers_it(
    host: FakeHost,
) -> None:
    (host.bin / "runsc").unlink()  # not installed yet: the fake apt-get provides it
    done = up(host, "--install-gvisor")
    assert done.code == 0, done.output
    calls = host.calls()
    assert any("curl" in c and "https://gvisor.dev/archive.key" in c for c in calls)
    assert any(c.startswith("gpg --dearmor") for c in calls)
    assert any(c.startswith("apt-get install") and "runsc" in c for c in calls)
    assert "runsc install" in calls
    assert "systemctl restart docker" in calls
    assert calls.index("runsc install") < calls.index("systemctl restart docker")
    sources = (host.apt_sources / "gvisor.list").read_text(encoding="utf-8")
    assert "https://storage.googleapis.com/gvisor/releases release main" in sources
    assert "signed-by=" in sources


# --- readiness ----------------------------------------------------------------------------------


def test_the_script_waits_for_readyz_and_fails_with_the_failing_checks(host: FakeHost) -> None:
    done = up(host, env={"FAKE_NOT_READY": "1"})
    assert done.code != 0
    assert "not ready" in done.stderr
    assert "build it on the host" in done.stderr
    assert len(host.calls_matching(" exec ")) == 60, "bounded: one try per attempt, no more"


def test_an_https_that_is_not_answering_yet_is_a_warning_not_a_failure(host: FakeHost) -> None:
    done = up(host, env={"FAKE_HTTPS_RC": "1"})
    assert done.code == 0, done.output
    assert "not answering with a valid certificate" in done.stderr


def test_a_scripted_rehearsal_needs_no_key_and_says_so_loudly(host: FakeHost) -> None:
    done = host.run(
        "--up",
        "--no-pull",
        "--email",
        EMAIL,
        "--scripted",
        "--runtime",
        "runc",
        "--access-code-file",
        str(host.code_file()),
    )
    assert done.code == 0, done.output
    assert host.env_values()["LLM_PROVIDER"] == "scripted"
    assert "SCRIPTED MODELS" in done.stderr
    assert mode_of(host.etc / "llm-api-key") == 0o600


# --- down and destroy ---------------------------------------------------------------------------


def test_down_stops_the_stack_and_removes_sandboxes_but_keeps_data(host: FakeHost) -> None:
    assert up(host).code == 0
    (host.state / "data" / "sandcage.db").write_text("kept", encoding="utf-8")
    done = host.run("--down", env={"FAKE_SANDBOXES": "abc123\ndef456\n"})
    assert done.code == 0, done.output
    assert host.calls_matching(" down --remove-orphans")
    assert host.calls_matching("docker rm -f abc123 def456")
    assert (host.state / "data" / "sandcage.db").read_text(encoding="utf-8") == "kept"
    assert host.env_file.exists()


@pytest.mark.parametrize("typed", ["", "yes", "destroy", "Destroy Sandcage Data", "no\n"])
def test_destroy_data_deletes_nothing_without_the_exact_phrase(host: FakeHost, typed: str) -> None:
    assert up(host).code == 0
    (host.state / "data" / "sandcage.db").write_text("precious", encoding="utf-8")
    done = host.run("--destroy-data", stdin=typed + "\n")
    assert done.code != 0
    assert "not confirmed" in done.stderr
    assert (host.state / "data" / "sandcage.db").read_text(encoding="utf-8") == "precious"
    assert not host.calls_matching(" down --volumes")


def test_destroy_data_with_the_phrase_removes_data_work_and_volumes_but_not_secrets(
    host: FakeHost,
) -> None:
    assert up(host).code == 0
    (host.state / "data" / "sandcage.db").write_text("gone", encoding="utf-8")
    (host.state / "work" / "sandcage-leftover").mkdir()
    done = host.run(
        "--destroy-data", stdin="destroy sandcage data\n", env={"FAKE_SANDBOXES": "x1\n"}
    )
    assert done.code == 0, done.output
    assert not (host.state / "data").exists()
    assert not (host.state / "work").exists()
    assert host.calls_matching(" down --volumes --remove-orphans")
    assert host.calls_matching("docker rm -f x1")
    assert host.env_file.exists(), "secrets stay; the runbook says how to remove them"


def test_destroy_data_refuses_a_state_directory_that_is_too_shallow(host: FakeHost) -> None:
    done = host.run(
        "--destroy-data", stdin="destroy sandcage data\n", env={"SANDCAGE_STATE_DIR": "/x"}
    )
    assert done.code != 0
    assert "refusing to delete" in done.stderr


# --- arguments ----------------------------------------------------------------------------------


def test_no_action_prints_usage_and_does_nothing(host: FakeHost) -> None:
    done = host.run()
    assert done.code == 2
    assert "usage: deploy.sh" in done.stderr
    assert not host.calls()
    assert not host.etc.exists()
    assert not (host.state / "work").exists()


def test_two_actions_are_refused(host: FakeHost) -> None:
    done = host.run("--up", "--down")
    assert done.code != 0
    assert "exactly one action" in done.stderr
    assert not host.calls()


def test_an_unknown_option_is_refused(host: FakeHost) -> None:
    done = host.run("--up", "--password", "x")
    assert done.code != 0
    assert "unknown option" in done.stderr
    assert not host.calls()


def test_help_prints_usage_and_exits_zero(host: FakeHost) -> None:
    done = host.run("--help")
    assert done.code == 0
    assert "--destroy-data" in done.stdout
    assert not host.calls()


# --- a partial download cannot run half a script ------------------------------------------------


def test_a_script_cut_off_anywhere_before_its_last_line_does_nothing(
    host: FakeHost, tmp_path: Path
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    last_line_start = source.rindex("\nmain ") + 1
    cuts = sorted(
        {
            *range(1, last_line_start, max(1, last_line_start // 120)),
            *range(last_line_start - 40, last_line_start + len("main")),
        }
    )
    assert len(cuts) > 100
    for cut in cuts:
        partial = tmp_path / "partial.sh"
        partial.write_text(source[:cut], encoding="utf-8")
        before = host.snapshot()
        done = host.run("--up", "--no-pull", "--email", EMAIL, script=partial)
        assert not host.calls(), f"cut at byte {cut} ran a command: {host.calls()}"
        assert host.snapshot() == before, f"cut at byte {cut} changed the host"
        assert not host.etc.exists(), f"cut at byte {cut} created {host.etc}"
        assert not done.stdout, f"cut at byte {cut} printed {done.stdout!r}"


def test_all_functions_defined_and_main_never_called_does_nothing(
    host: FakeHost, tmp_path: Path
) -> None:
    """Everything but the last line: every function is defined, nothing is run."""
    source = SCRIPT.read_text(encoding="utf-8")
    body_only = tmp_path / "functions-only.sh"
    body_only.write_text(source[: source.rindex("\nmain ") + 1], encoding="utf-8")
    done = host.run("--up", "--email", EMAIL, script=body_only)
    assert done.code == 0
    assert not done.output
    assert not host.calls()
    assert not host.etc.exists()


def test_the_last_line_is_the_only_call_and_ends_the_script() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.rstrip().endswith('\nmain "$@"; exit')
    assert source.count('main "$@"') == 1


# --- the script updating its own checkout -------------------------------------------------------


def git(*args: str, cwd: Path, host: FakeHost) -> None:
    subprocess.run(  # noqa: S603 - fixed arguments in a scratch directory
        [shutil.which("git") or "git", *args],
        cwd=cwd,
        env=host.environment(),
        check=True,
        capture_output=True,
    )


def make_origin(host: FakeHost, tmp_path: Path) -> tuple[Path, Path]:
    """A scratch origin holding the kit; returns (origin work tree, bare origin)."""
    origin = tmp_path / "origin-work"
    (origin / "deploy").mkdir(parents=True)
    (origin / "src" / "sandcage" / "sandbox" / "image").mkdir(parents=True)
    (origin / "src" / "sandcage" / "sandbox" / "image" / "Dockerfile").write_text("FROM scratch\n")
    (origin / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    shutil.copy(SCRIPT, origin / "deploy" / "deploy.sh")
    git("init", "-q", "-b", "main", cwd=origin, host=host)
    git("add", "-A", cwd=origin, host=host)
    git("commit", "-q", "-m", "first", cwd=origin, host=host)
    bare = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603 - scratch repository
        [shutil.which("git") or "git", "clone", "-q", "--bare", str(origin), str(bare)],
        check=True,
        capture_output=True,
    )
    git("remote", "add", "origin", str(bare), cwd=origin, host=host)
    return origin, bare


def test_the_script_survives_pulling_a_new_version_of_itself(
    host: FakeHost, tmp_path: Path
) -> None:
    origin, bare = make_origin(host, tmp_path)
    checkout = tmp_path / "checkout"
    subprocess.run(  # noqa: S603 - scratch repository
        [shutil.which("git") or "git", "clone", "-q", str(bare), str(checkout)],
        check=True,
        capture_output=True,
    )
    # The next commit rewrites the script: a long block near the top moves every later byte.
    script = origin / "deploy" / "deploy.sh"
    text = script.read_text(encoding="utf-8")
    padding = "# padding so every later byte offset moves\n" * 200
    script.write_text(text.replace("set -euo pipefail\n", "set -euo pipefail\n" + padding, 1))
    git("commit", "-q", "-am", "edit the script itself", cwd=origin, host=host)
    git("push", "-q", "origin", "main", cwd=origin, host=host)

    installed = checkout / "deploy" / "deploy.sh"
    before = installed.read_text(encoding="utf-8")
    done = host.run(
        "--up",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(host.key_file()),
        env={"SANDCAGE_APP_DIR": str(checkout)},
        script=installed,
    )
    assert installed.read_text(encoding="utf-8") != before, "the pull did not change the script"
    assert done.code == 0, done.output
    values = host.env_values()
    assert all(values[name] for name in ("SITE_ADDRESS", "DEMO_ACCESS_CODE", "DOCKER_GID"))
    assert host.calls_matching(" up -d --build --remove-orphans")
    assert "deploy.sh itself changed" in done.stderr
    assert "repository: main at" in done.stdout


def test_a_first_run_can_clone_the_repository_with_a_read_only_deploy_key(
    host: FakeHost, tmp_path: Path
) -> None:
    _, bare = make_origin(host, tmp_path)
    target = tmp_path / "fresh-checkout"
    key = host.private_file("deploy-key", "FAKE-DEPLOY-KEY-CONTENT")
    done = host.run(
        "--up",
        "--repo",
        str(bare),
        "--deploy-key",
        str(key),
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file()),
        "--llm-key-file",
        str(host.key_file()),
        env={"SANDCAGE_APP_DIR": str(target)},
    )
    assert done.code == 0, done.output
    assert (target / "docker-compose.yml").exists()
    assert "repository: cloned main" in done.stdout
    assert host.env_values()["DEPLOY_KEY_FILE"] == str(key)


def test_a_deploy_key_that_others_can_read_is_refused(host: FakeHost) -> None:
    key = host.private_file("deploy-key", "FAKE", 0o644)
    done = host.run("--up", "--deploy-key", str(key), "--email", EMAIL)
    assert done.code != 0
    assert "mode" in done.stderr


def test_no_checkout_and_no_repository_is_a_clear_error(host: FakeHost, tmp_path: Path) -> None:
    done = host.run(
        "--up",
        "--email",
        EMAIL,
        "--access-code-file",
        str(host.code_file()),
        env={"SANDCAGE_APP_DIR": str(tmp_path / "nowhere")},
    )
    assert done.code != 0
    assert "no checkout" in done.stderr


# --- static rules -------------------------------------------------------------------------------


def test_the_script_never_prints_or_traces_a_secret_variable() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "set -x" not in source
    assert "xtrace" not in source
    printing = re.compile(r"^\s*(echo|say|warn|die)\b.*(CODE|KEY|REPLY_VALUE|ASKED_VALUE)", re.M)
    for found in printing.finditer(source):
        line = found.group(0).strip()
        # Names of files and flags are fine; a secret variable's value is not.
        assert not re.search(
            r"\$\{?(CHOSEN_CODE|GENERATED_CODE|REPLY_VALUE|ASKED_VALUE|key)\b", line
        ), line
    # The one place the code is printed is the terminal, once.
    shown = [i for i, line in enumerate(source.splitlines()) if '"$CHOSEN_CODE"' in line]
    for index in shown:
        window = "\n".join(source.splitlines()[max(0, index - 6) : index + 6])
        assert "/dev/tty" in window or "DEMO_ACCESS_CODE=%s" in window or "valid_code" in window


def test_the_script_is_executable_and_uses_strict_mode() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in source
