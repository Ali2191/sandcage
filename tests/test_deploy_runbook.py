"""deploy/RUNBOOK.md is copy-paste material: its code blocks parse, every option it uses exists,
every file it names exists, and the only placeholders are the ones it declares."""

from __future__ import annotations

import re
import subprocess

from tests.deploy_helpers import DEPLOY_DIR, REPO

RUNBOOK = DEPLOY_DIR / "RUNBOOK.md"
DECLARED = {"<VM_IP>", "<YOUR_IP>", "<YOUR_EMAIL>", "<YOUR_DOMAIN_NAME>", "<VM_IP with dashes>"}
SUBSTITUTES = {
    "<VM_IP with dashes>": "203-0-113-7",
    "<VM_IP>": "203.0.113.7",
    "<YOUR_IP>": "198.51.100.4",
    "<YOUR_EMAIL>": "me@example.com",
    "<YOUR_DOMAIN_NAME>": "demo.example.com",
}


def text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def shell_blocks() -> list[str]:
    return re.findall(r"```sh\n(.*?)```", text(), re.DOTALL)


def test_the_only_placeholders_are_the_declared_ones() -> None:
    found = set(re.findall(r"<[A-Z][A-Z_]*(?: [a-z ]+)?>", text()))
    assert found <= DECLARED, f"undeclared placeholders: {sorted(found - DECLARED)}"
    assert {"<VM_IP>", "<YOUR_IP>", "<YOUR_EMAIL>"} <= found


def test_no_unfinished_markers() -> None:
    assert not re.search(r"\b(TODO|TBD|FIXME|XXX|lorem)\b", text(), re.IGNORECASE)


def test_every_code_block_parses_as_shell() -> None:
    blocks = shell_blocks()
    assert len(blocks) >= 20
    for block in blocks:
        script = block
        for placeholder, value in SUBSTITUTES.items():
            script = script.replace(placeholder, value)
        done = subprocess.run(
            ["/bin/bash", "-n"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"{done.stderr}\n--- in block:\n{block}"


def test_every_deploy_script_option_it_uses_exists() -> None:
    usage = (DEPLOY_DIR / "deploy.sh").read_text(encoding="utf-8")
    used: set[str] = set()
    for block in shell_blocks():
        flat = block.replace("\\\n", " ")
        for line in flat.splitlines():
            if "deploy/deploy.sh" in line:
                used.update(re.findall(r"\s(--[a-z-]+)", line))
    assert {"--up", "--down", "--destroy-data", "--generate-code", "--email"} <= used
    for option in used:
        assert f"  {option}" in usage or f"{option})" in usage, f"deploy.sh has no {option}"


def test_every_verify_option_it_uses_exists() -> None:
    script = (REPO / "scripts" / "verify_deploy.py").read_text(encoding="utf-8")
    for block in shell_blocks():
        for line in block.replace("\\\n", " ").splitlines():
            if "verify_deploy.py" in line:
                for option in re.findall(r"\s(--[a-z-]+)", line):
                    assert f'"{option}"' in script, f"verify_deploy.py has no {option}"


def test_the_files_it_names_exist() -> None:
    for name in re.findall(
        r"(?:\./)?((?:deploy|scripts)/[A-Za-z_./-]+\.(?:sh|py)|deploy/Caddyfile)", text()
    ):
        assert (REPO / name).exists(), f"{name} does not exist"


def test_it_covers_every_step_the_handoff_lists() -> None:
    headings = re.findall(r"^## \(([a-l])\)", text(), re.MULTILINE)
    assert headings == list("abcdefghijkl")


def test_it_contains_the_safety_rules() -> None:
    body = text()
    assert "never paste" in body.lower() or "Never paste" in body
    assert "destroy it (below) as soon as you no longer need it" in body
    assert "self-hosted deployment" in body and "no hosted public instance" in body
    assert "$0.06 per hour" in body
    assert "Auto Backups** **off" in body or "Auto Backups **off**" in body
    assert "Allow write access" in body
    assert "hidden prompt" in body


def test_the_deploy_script_help_matches_the_options_the_runbook_relies_on() -> None:
    done = subprocess.run(  # noqa: S603 - the script's own --help, no side effects
        ["/bin/bash", str(DEPLOY_DIR / "deploy.sh"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0
    for option in ("--generate-code", "--deploy-key", "--site", "--no-pull", "--rotate-code"):
        assert option in done.stdout
