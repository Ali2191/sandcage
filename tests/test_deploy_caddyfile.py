"""The Caddyfile keeps the edge rules (DEPLOY.md). The structural tests read the file; the
docker-marked one asks Caddy itself whether the file is valid."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.deploy_helpers import CADDYFILE, DOCKERFILES, REPO


def caddy_image() -> str:
    for path in DOCKERFILES:
        found = re.search(r"^FROM\s+(caddy\S+)", path.read_text(encoding="utf-8"), re.MULTILINE)
        if found is not None:
            return found.group(1)
    raise AssertionError("deploy/caddy.Dockerfile has no caddy base image")


def docker_binary() -> str:
    found = shutil.which("docker")
    assert found is not None, "the docker-marked tests need Docker"
    return found


def text() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


def block(source: str, opening: str) -> str:
    """The text between the braces of the block whose first line is exactly ``opening`` (an
    indented line is matched with its indentation; ``opening`` ends with the opening brace)."""
    offset = 0
    for line in source.splitlines(keepends=True):
        if line.rstrip() == opening or (not opening.startswith("\t") and line.startswith(opening)):
            break
        offset += len(line)
    else:
        raise AssertionError(f"no line {opening!r}")
    opened = offset + len(line.rstrip()) - 1
    assert source[opened] == "{", f"{opening!r} does not open a block on its line"
    depth = 0
    for index in range(opened, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opened + 1 : index]
    raise AssertionError(f"unbalanced braces after {opening!r}")


def directives(source: str) -> list[str]:
    return [line.strip() for line in source.splitlines() if line.strip() and line.strip()[0] != "#"]


def test_the_site_name_and_the_certificate_email_come_from_the_environment() -> None:
    source = text()
    assert "email {$ACME_EMAIL}" in source
    assert re.search(r"^\{\$SITE_ADDRESS\} \{", source, re.MULTILINE)
    assert re.search(r"^http://\{\$SITE_ADDRESS\} \{", source, re.MULTILINE)


def test_http_redirects_to_https_with_no_server_header() -> None:
    source = text()
    assert "auto_https disable_redirects" in source
    plain = directives(block(source, "http://{$SITE_ADDRESS} {"))
    assert plain == ["header -Server", "redir https://{host}{uri} permanent"]


def test_only_http_1_and_2_are_served_so_no_udp_port_is_needed() -> None:
    assert "protocols h1 h2" in text()


def test_slow_clients_are_cut_off_at_the_edge() -> None:
    timeouts = directives(block(block(text(), "\tservers {"), "\t\ttimeouts {"))
    assert timeouts == ["read_header 10s", "read_body 20s", "idle 2m"]
    assert not any(line.startswith("write") for line in timeouts), "a write timeout kills streams"


def test_the_api_route_strips_the_prefix_caps_bodies_and_never_buffers_or_compresses() -> None:
    api = block(text(), "\thandle_path /api/* {")
    assert "request_body {\n\t\t\tmax_size 64KB" in api
    assert "encode" not in api, (
        "no compression on the API: event streams must arrive event by event"
    )
    for upstream in re.findall(r"reverse_proxy (\S+)", api):
        assert upstream == "api:8000"
    assert api.count("flush_interval -1") == api.count("reverse_proxy api:8000")
    assert api.count("compression off") == api.count("reverse_proxy api:8000")
    assert api.count("health_uri /healthz") == api.count("reverse_proxy api:8000")


def test_long_read_timeouts_apply_to_the_event_stream_only() -> None:
    api = block(text(), "\thandle_path /api/* {")
    stream = block(api, "\t\thandle @events {")
    other = block(api, "\t\thandle {")
    assert "read_timeout 60s" in stream
    assert "response_header_timeout" not in stream
    assert "read_timeout 30s" in other
    assert "response_header_timeout 30s" in other
    assert "@events path_regexp ^/runs/[0-9a-f]{12}/events$" in api


def test_the_panel_route_compresses_and_sets_a_strict_content_security_policy() -> None:
    panel = block(text(), "\thandle {")
    assert "encode zstd gzip" in panel
    assert "reverse_proxy panel:3000" in panel
    policy = re.search(r'header Content-Security-Policy "([^"]+)"', panel)
    assert policy is not None
    rules = dict(part.strip().split(" ", 1) for part in policy.group(1).split(";"))
    assert rules["default-src"] == "'self'"
    assert rules["script-src"] == "'self' 'unsafe-inline'"
    assert rules["connect-src"] == "'self'"
    assert rules["object-src"] == "'none'"
    assert rules["frame-ancestors"] == "'none'"
    assert rules["base-uri"] == "'self'"
    assert rules["form-action"] == "'self'"
    assert "unsafe-eval" not in policy.group(1)
    assert "*" not in policy.group(1)
    assert "http:" not in policy.group(1)


def test_security_headers_and_no_server_header() -> None:
    headers = directives(block(block(text(), "{$SITE_ADDRESS} {"), "\theader {"))
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in headers
    for wanted in (
        "X-Content-Type-Options nosniff",
        "X-Frame-Options DENY",
        "Referrer-Policy no-referrer",
        "-Server",
    ):
        assert wanted in headers
    assert any(line.startswith("Permissions-Policy") for line in headers)


def test_the_access_log_drops_every_header_and_the_whole_query_string() -> None:
    log = block(block(text(), "{$SITE_ADDRESS} {"), "\tlog {")
    assert "output stdout" in log
    assert "request>headers delete" in log
    assert 'request>uri regexp \\?.* ""' in log
    assert "resp_headers delete" in log


def test_no_upstream_other_than_the_api_and_the_panel() -> None:
    upstreams = set(re.findall(r"reverse_proxy (\S+)", text()))
    assert upstreams == {"api:8000", "panel:3000"}


@pytest.mark.docker
def test_caddy_itself_accepts_the_file() -> None:
    done = subprocess.run(  # noqa: S603 - fixed arguments, a pinned image
        [
            docker_binary(),
            "run",
            "--rm",
            "-e",
            "ACME_EMAIL=ops@example.com",
            "-e",
            "SITE_ADDRESS=demo.example.com",
            "-v",
            f"{REPO / 'deploy' / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
            caddy_image(),
            "caddy",
            "validate",
            "--config",
            "/etc/caddy/Caddyfile",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert "Valid configuration" in done.stdout + done.stderr


@pytest.mark.docker
def test_caddy_rejects_a_file_without_the_required_settings() -> None:
    """Proves the validation test above can fail: an empty ACME_EMAIL is refused."""
    done = subprocess.run(  # noqa: S603 - fixed arguments, a pinned image
        [
            docker_binary(),
            "run",
            "--rm",
            "-e",
            "SITE_ADDRESS=demo.example.com",
            "-v",
            f"{REPO / 'deploy' / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
            caddy_image(),
            "caddy",
            "validate",
            "--config",
            "/etc/caddy/Caddyfile",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode != 0
