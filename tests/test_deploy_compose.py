"""The compose file keeps the deploy rules (DEPLOY.md): one test per rule, so a regression names
the rule it broke. No Docker is needed: the file is parsed, never run."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.deploy_helpers import (
    COMPOSE_FILE,
    DEPLOY_DIR,
    DOCKERFILES,
    REPO,
    environment,
    load_compose,
    reference_names,
    services,
)

SERVICE_NAMES = {"caddy", "panel", "api", "dockerproxy"}
SECRET_WORDS = frozenset({"KEY", "CODE", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL"})
# What a real key or token looks like (long runs of key characters, vendor prefixes).
KEY_SHAPES = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|[A-Za-z0-9+/_-]{40,}"
)


def is_secret_name(name: str) -> bool:
    """A variable whose name has a word that says it holds a secret (WRITEBYTECODE does not)."""
    return bool(SECRET_WORDS & set(name.upper().split("_")))


def test_the_four_services_exist_and_nothing_else() -> None:
    assert set(services()) == SERVICE_NAMES


def test_only_the_proxy_mounts_the_docker_socket() -> None:
    for name, service in services().items():
        mounts = [str(v) for v in service.get("volumes", [])]
        has_socket = any("docker.sock" in mount or "/var/run/docker" in mount for mount in mounts)
        assert has_socket == (name == "dockerproxy"), f"{name}: Docker socket rule broken"
    proxy_mounts = [str(v) for v in services()["dockerproxy"]["volumes"]]
    assert len(proxy_mounts) == 1, "the proxy mounts the socket and nothing else"


def test_only_caddy_publishes_ports_and_only_80_and_443() -> None:
    for name, service in services().items():
        if name == "caddy":
            continue
        assert "ports" not in service, f"{name} publishes a port"
    published = {str(p) for p in services()["caddy"]["ports"]}
    assert published == {"80:80", "443:443"}


def test_no_service_uses_privileged_modes() -> None:
    forbidden = ("privileged", "network_mode", "pid", "ipc", "userns_mode", "devices", "cap_add")
    for name, service in services().items():
        for key in forbidden:
            assert key not in service, f"{name} sets {key}"
        for opt in service.get("security_opt", []):
            assert not str(opt).startswith(("seccomp=unconfined", "apparmor=unconfined")), name
    assert "host" not in str(load_compose().get("networks", {}))


@pytest.mark.parametrize("name", sorted(SERVICE_NAMES))
def test_every_service_is_hardened(name: str) -> None:
    service = services()[name]
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["read_only"] is True
    uid = str(service["user"]).split(":", maxsplit=1)[0]
    assert uid.isdigit(), "run as a numeric uid"
    assert int(uid) != 0, "run as a non-root uid"
    assert service["mem_limit"]
    assert service["memswap_limit"] == service["mem_limit"], "no swap beyond the memory limit"
    assert int(service["pids_limit"]) > 0
    assert service["healthcheck"]["test"]
    assert service["restart"] == "unless-stopped"


def test_caddy_binds_low_ports_without_a_capability() -> None:
    caddy = services()["caddy"]
    assert caddy["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": "0"}
    assert caddy["cap_drop"] == ["ALL"]


def test_no_secret_value_is_written_in_the_compose_file() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not KEY_SHAPES.search("\n".join(code_lines)), "something that looks like a key"
    for name, service in services().items():
        for variable, value in environment(service).items():
            if is_secret_name(variable):
                # A reference to the env file, or the path of a mounted secret: never a value.
                assert value.startswith(("${", "/run/secrets/")), f"{name}.{variable} is a value"
    assert "env_file" not in text, "compose passes variables, never an env file to a service"


def test_no_service_mounts_an_env_file_or_the_deploy_secrets_directory() -> None:
    for name, service in services().items():
        for mount in map(str, service.get("volumes", [])):
            assert "/etc/sandcage" not in mount, f"{name} mounts the settings directory"
            assert not re.search(r"\.env\b", mount), f"{name} mounts an env file"


def test_the_model_key_is_a_secret_file_and_never_an_environment_variable() -> None:
    compose = load_compose()
    assert list(compose["secrets"]) == ["llm_api_key"]
    assert services()["api"]["secrets"] == ["llm_api_key"]
    for name, service in services().items():
        if name != "api":
            assert "secrets" not in service
        assert "LLM_API_KEY" not in environment(service), name
    api = environment(services()["api"])
    assert api["SANDCAGE_KEY_FILE"] == "/run/secrets/llm_api_key"


def test_only_the_api_knows_the_provider_and_the_access_code() -> None:
    for name in ("caddy", "panel", "dockerproxy"):
        keys = set(environment(services()[name]))
        assert not {k for k in keys if k.startswith(("LLM_", "DEMO_"))}, name


def test_work_and_data_directories_have_the_same_path_on_host_and_in_the_api() -> None:
    api = services()["api"]
    mounts = {str(v) for v in api["volumes"]}
    assert "${SANDCAGE_WORK_DIR}:${SANDCAGE_WORK_DIR}" in mounts
    assert "${SANDCAGE_DATA_DIR}:${SANDCAGE_DATA_DIR}" in mounts
    env = environment(api)
    assert reference_names(env["TMPDIR"]) == {"SANDCAGE_WORK_DIR"}
    assert reference_names(env["SANDCAGE_DATA_DIR"]) == {"SANDCAGE_DATA_DIR"}
    proxy = environment(services()["dockerproxy"])
    assert reference_names(proxy["SANDCAGE_WORK_DIR"]) == {"SANDCAGE_WORK_DIR"}, (
        "the proxy allows exactly the bind mounts under the same work directory"
    )


def test_public_demo_settings_are_fixed_by_compose() -> None:
    env = environment(services()["api"])
    assert env["DEMO_MODE"] == "public"
    assert env["DEMO_TRUST_PROXY"] == "true"
    assert env["DEMO_ACCESS_CODE"].startswith("${DEMO_ACCESS_CODE:?"), "required, never defaulted"
    assert env["LLM_BASE_URL"] == "https://api.vultrinference.com/v1"
    assert env["LLM_MODEL"] == "${LLM_MODEL:-deepseek-v4.1-flash-1017}"
    assert "LLM_RETRIES" in env
    assert "PANEL_ORIGIN" not in env, "same origin through Caddy: no CORS"
    assert env["SANDCAGE_API_HOST"] == "0.0.0.0"  # noqa: S104 - an internal-only network
    assert env["DOCKER_HOST"] == "tcp://dockerproxy:2375"


def test_the_bench_results_folder_is_mounted_and_persisted() -> None:
    api = services()["api"]
    env = environment(api)
    mounts = {str(v) for v in api["volumes"]}
    assert "./benchmarks/out:/bench:ro" in mounts
    assert env["SANDCAGE_BENCH_RESULTS"] == "/bench/results.json"


def test_the_panel_is_built_for_the_same_origin() -> None:
    build = services()["panel"]["build"]
    assert build["args"] == {"NEXT_PUBLIC_API_URL": "/api"}


def test_required_settings_stop_compose_with_a_message_naming_them() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    for name in (
        "SITE_ADDRESS",
        "ACME_EMAIL",
        "DEMO_ACCESS_CODE",
        "SANDCAGE_WORK_DIR",
        "SANDCAGE_DATA_DIR",
        "DOCKER_GID",
        "SANDCAGE_LLM_KEY_FILE",
    ):
        assert f"${{{name}:?" in text, f"{name} must be required with a message"


def test_networks_internal_where_they_must_be() -> None:
    compose = load_compose()
    networks = compose["networks"]
    assert networks["app"]["internal"] is True
    assert networks["dockerapi"]["internal"] is True
    assert not networks["edge"].get("internal")
    attached = {name: set(service["networks"]) for name, service in services().items()}
    assert attached == {
        "caddy": {"edge", "app"},
        "panel": {"app"},
        "api": {"app", "egress", "dockerapi"},
        "dockerproxy": {"dockerapi"},
    }, "the proxy sees only the internal docker network; only the API has a route out"


def test_start_order_waits_for_health() -> None:
    caddy = services()["caddy"]["depends_on"]
    assert {k: v["condition"] for k, v in caddy.items()} == {
        "api": "service_healthy",
        "panel": "service_healthy",
    }
    assert services()["api"]["depends_on"]["dockerproxy"]["condition"] == "service_healthy"


def test_the_api_has_a_fixed_hostname_for_orphan_reaping() -> None:
    assert services()["api"]["hostname"] == "sandcage-api"


def test_the_proxy_runs_the_same_image_and_joins_only_the_sockets_group() -> None:
    proxy = services()["dockerproxy"]
    assert proxy["image"] == services()["api"]["image"]
    assert proxy["command"][-1] == "sandcage.dockerproxy"
    assert proxy["group_add"] == [
        "${DOCKER_GID:?set DOCKER_GID (the group id of /var/run/docker.sock) in the env file}"
    ]


# --- the images -------------------------------------------------------------------------------


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_every_base_image_is_pinned_by_digest(dockerfile: object) -> None:
    from pathlib import Path

    text = Path(str(dockerfile)).read_text(encoding="utf-8")
    froms = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    assert froms
    for image in froms:
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), f"{image} is not pinned by digest"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_no_image_layer_carries_a_secret_or_an_env_file(dockerfile: object) -> None:
    from pathlib import Path

    lines = [
        line
        for line in Path(str(dockerfile)).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    for line in lines:
        if re.match(r"\s*(ENV|ARG)\b", line):
            for variable in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", line):
                assert not is_secret_name(variable), f"an image layer would hold {variable}"
        if re.match(r"\s*(COPY|ADD)\b", line):
            assert not re.search(r"\.env\b", line), f"copies an env file: {line}"
            assert "--from=" in line or ". " not in line + " ", f"copies the whole context: {line}"


def test_the_build_context_never_includes_an_env_file() -> None:
    patterns = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    active = [p.strip() for p in patterns if p.strip() and not p.startswith("#")]
    assert {"**/.env", "**/.env.*", "**/*.env", ".dev.env", ".git"} <= set(active)
    # The only exception is the demo repo's deliberately fake .env, which the demo needs.
    exceptions = [p for p in active if p.startswith("!")]
    assert exceptions == ["!demo-repos/invoice-calc/.env"]


def test_the_api_image_holds_only_the_code_the_api_needs() -> None:
    text = (DEPLOY_DIR / "api.Dockerfile").read_text(encoding="utf-8")
    copies = re.findall(r"^COPY\s+(.+)$", text, re.MULTILINE)
    assert copies == [
        "deploy/requirements.txt /tmp/requirements.txt",
        "src ./src",
        "demo-repos ./demo-repos",
        "scripts/api_with_key_file.py ./scripts/api_with_key_file.py",
    ]
    assert re.search(r"^USER 10001:10001$", text, re.MULTILINE)
    assert "--require-hashes" in text


def test_requirements_match_the_lock_file() -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is needed to check deploy/requirements.txt"
    exported = subprocess.run(  # noqa: S603 - fixed arguments
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    wanted = [line for line in exported.splitlines() if not line.startswith("#")]
    have = [
        line
        for line in (DEPLOY_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    assert have == wanted, "deploy/requirements.txt is stale: re-run the uv export in its header"
