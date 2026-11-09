"""The Docker proxy allowlist: the real sandbox requests pass, everything else is refused."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sandcage.dockerproxy.policy import (
    ProxyConfig,
    ProxyConfigError,
    check_request,
    is_empty,
)
from sandcage.sandbox.hardening import OwnerIdentity, hardened_config

WORK = "/srv/sandcage/work"
CONFIG = ProxyConfig("sandcage-sandbox:latest", WORK)
IDENTITY = OwnerIdentity("api", 1, "abc")
WORKSPACE = Path(f"{WORK}/sandcage-x1y2/workspace")


def sandbox_create() -> dict[str, object]:
    return hardened_config(
        command=["sleep", "infinity"], identity=IDENTITY, workspace=WORKSPACE, runtime=None
    )


def create(body: object, query: str = "") -> tuple[bool, str]:
    target = "/v1.44/containers/create" + (f"?{query}" if query else "")
    decision = check_request("POST", target, json.dumps(body).encode(), CONFIG)
    return decision.allowed, decision.reason


def with_host(**changes: object) -> dict[str, object]:
    body = copy.deepcopy(sandbox_create())
    host = body["HostConfig"]
    assert isinstance(host, dict)
    for key, value in changes.items():
        if value is ...:
            host.pop(key, None)
        else:
            host[key] = value
    return body


def with_top(**changes: object) -> dict[str, object]:
    body = copy.deepcopy(sandbox_create())
    for key, value in changes.items():
        if value is ...:
            body.pop(key, None)
        else:
            body[key] = value
    return body


# --- what the API really sends is allowed --------------------------------------------


def test_the_real_sandbox_create_request_is_allowed() -> None:
    assert create(sandbox_create()) == (True, "allowed")


def test_the_gvisor_sandbox_and_the_runtime_probe_are_allowed() -> None:
    gvisor = hardened_config(
        command=["sleep", "infinity"], identity=IDENTITY, workspace=WORKSPACE, runtime="runsc"
    )
    probe = hardened_config(command=["dmesg"], identity=IDENTITY, workspace=None, runtime="runsc")
    assert create(gvisor)[0]
    assert create(probe)[0]


def test_the_sdk_style_empty_defaults_are_tolerated() -> None:
    body = with_top(Entrypoint=None, Volumes={}, ExposedPorts=None, Hostname="", StopSignal="")
    host = body["HostConfig"]
    assert isinstance(host, dict)
    host.update(
        {
            "Privileged": False,
            "CapAdd": None,
            "Devices": [],
            "RestartPolicy": {"Name": "", "MaximumRetryCount": 0},
            "ConsoleSize": [0, 0],
        }
    )
    assert create(body) == (True, "allowed")


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("GET", "/_ping"),
        ("HEAD", "/_ping"),
        ("GET", "/v1.44/version"),
        ("GET", "/info"),
        ("GET", "/v1.44/images/sandcage-sandbox:latest/json"),
        ("GET", '/containers/json?all=1&filters={"label":["sandcage=sandbox"]}'),
    ],
)
def test_harmless_reads_are_allowed(method: str, target: str) -> None:
    decision = check_request(method, target, b"", CONFIG)
    assert decision.allowed, decision.reason
    assert decision.container is None


@pytest.mark.parametrize(
    ("method", "target", "body"),
    [
        ("GET", "/v1.44/containers/abc123/json", b""),
        ("POST", "/v1.44/containers/abc123/start", b""),
        ("POST", "/containers/abc123/stop?t=1", b""),
        ("POST", "/containers/abc123/kill?signal=KILL", b""),
        ("POST", "/containers/abc123/wait", b""),
        ("GET", "/containers/abc123/logs?stdout=1&stderr=1", b""),
        ("DELETE", "/v1.44/containers/abc123?force=1&v=1", b""),
        (
            "POST",
            "/v1.44/containers/abc123/exec",
            json.dumps(
                {
                    "Cmd": ["sh", "-c", "pytest -q"],
                    "User": "65534:65534",
                    "AttachStdin": False,
                    "AttachStdout": True,
                    "AttachStderr": True,
                    "Tty": False,
                    "WorkingDir": "/workspace",
                }
            ).encode(),
        ),
    ],
)
def test_container_actions_need_an_upstream_label_check(
    method: str, target: str, body: bytes
) -> None:
    decision = check_request(method, target, body, CONFIG)
    assert decision.allowed, decision.reason
    assert decision.container == "abc123"


@pytest.mark.parametrize(
    ("method", "target", "body"),
    [
        ("POST", "/v1.44/exec/e1/start", b'{"Detach": false, "Tty": false}'),
        ("GET", "/v1.44/exec/e1/json", b""),
    ],
)
def test_exec_actions_need_a_sandbox_exec(method: str, target: str, body: bytes) -> None:
    decision = check_request(method, target, body, CONFIG)
    assert decision.allowed, decision.reason
    assert decision.exec_id == "e1"


# --- unsafe creates are refused (the table) ------------------------------------------

REFUSED_CREATES: list[tuple[str, dict[str, object], str]] = [
    ("privileged", with_host(Privileged=True), "Privileged"),
    ("host bind", with_host(Binds=["/:/workspace"]), "Binds"),
    ("socket mount", with_host(Binds=["/var/run/docker.sock:/workspace"]), "Binds"),
    (
        "socket via Mounts",
        with_host(Mounts=[{"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/s"}]),
        "Mounts",
    ),
    ("bind to elsewhere", with_host(Binds=[f"{WORKSPACE}:/etc"]), "Binds"),
    ("bind of the work dir itself", with_host(Binds=[f"{WORK}:/workspace"]), "Binds"),
    ("bind escaping with dots", with_host(Binds=[f"{WORK}/../etc:/workspace"]), "Binds"),
    ("bind sibling prefix", with_host(Binds=[f"{WORK}-evil/x:/workspace"]), "Binds"),
    ("read-only bind flag swap", with_host(Binds=[f"{WORKSPACE}:/workspace:rw,z"]), "Binds"),
    ("two binds", with_host(Binds=[f"{WORKSPACE}:/workspace", f"{WORKSPACE}:/workspace"]), "Binds"),
    ("volumes from", with_host(VolumesFrom=["other"]), "VolumesFrom"),
    ("other image", with_top(Image="alpine:3.20"), "sandbox image"),
    ("missing image", with_top(Image=...), "Image"),
    ("root user", with_top(User="0:0"), "User"),
    ("no user", with_top(User=...), "User"),
    ("environment", with_top(Env=["AWS_SECRET_ACCESS_KEY=x"]), "Env"),
    ("network enabled", with_top(NetworkDisabled=False), "NetworkDisabled"),
    ("host network", with_host(NetworkMode="host"), "NetworkMode"),
    ("bridge network", with_host(NetworkMode="bridge"), "NetworkMode"),
    (
        "networking config",
        with_top(NetworkingConfig={"EndpointsConfig": {"bridge": {}}}),
        "NetworkingConfig",
    ),
    ("writable root", with_host(ReadonlyRootfs=False), "ReadonlyRootfs"),
    ("capabilities kept", with_host(CapDrop=["NET_RAW"]), "CapDrop"),
    ("capability added", with_host(CapAdd=["SYS_ADMIN"]), "CapAdd"),
    (
        "seccomp off",
        with_host(SecurityOpt=["no-new-privileges", "seccomp=unconfined"]),
        "SecurityOpt",
    ),
    ("apparmor off", with_host(SecurityOpt=["apparmor=unconfined"]), "SecurityOpt"),
    ("label off", with_host(SecurityOpt=["label=disable"]), "SecurityOpt"),
    ("no memory limit", with_host(Memory=...), "Memory"),
    ("too much memory", with_host(Memory=8 * 1024**3, MemorySwap=8 * 1024**3), "Memory"),
    ("extra swap", with_host(MemorySwap=-1), "MemorySwap"),
    ("no pid limit", with_host(PidsLimit=...), "PidsLimit"),
    ("no cpu limit", with_host(NanoCpus=...), "NanoCpus"),
    ("bad runtime", with_host(Runtime="kata"), "Runtime"),
    ("extra tmpfs", with_host(Tmpfs={"/tmp": "rw,noexec,nosuid", "/run": "rw"}), "Tmpfs"),
    ("exec tmpfs", with_host(Tmpfs={"/tmp": "rw,exec,nosuid"}), "Tmpfs"),
    ("host pid namespace", with_host(PidMode="host"), "PidMode"),
    ("host ipc", with_host(IpcMode="host"), "IpcMode"),
    ("host uts", with_host(UTSMode="host"), "UTSMode"),
    ("userns host", with_host(UsernsMode="host"), "UsernsMode"),
    ("cgroup host", with_host(CgroupnsMode="host"), "CgroupnsMode"),
    ("devices", with_host(Devices=[{"PathOnHost": "/dev/kvm"}]), "Devices"),
    ("device requests", with_host(DeviceRequests=[{"Count": -1}]), "DeviceRequests"),
    ("sysctls", with_host(Sysctls={"net.ipv4.ip_forward": "1"}), "Sysctls"),
    ("masked paths cleared", with_host(MaskedPaths=["/proc/kcore"]), "MaskedPaths"),
    ("ports", with_host(PortBindings={"80/tcp": [{"HostPort": "80"}]}), "PortBindings"),
    ("restart policy", with_host(RestartPolicy={"Name": "always"}), "RestartPolicy"),
    ("label missing", with_top(Labels={"app": "x"}), "Labels"),
    ("foreign label", with_top(Labels={"sandcage": "sandbox", "com.docker.x": "y"}), "Labels"),
    ("attach stdin", with_top(OpenStdin=True), "OpenStdin"),
    ("entrypoint override", with_top(Entrypoint=["/bin/sh"]), "Entrypoint"),
    ("unknown top field", with_top(Healthcheck={"Test": ["CMD", "curl", "x"]}), "Healthcheck"),
    ("anonymous volume", with_top(Volumes={"/data": {}}), "Volumes"),
    (
        "nested empty network",
        with_host(
            NetworkMode="none", Links=[], Mounts=None, LogConfig={"Type": "json-file", "Config": {}}
        ),
        "LogConfig",
    ),
    ("no host config", with_top(HostConfig=...), "HostConfig"),
]


@pytest.mark.parametrize(
    ("name", "body", "word"), REFUSED_CREATES, ids=[name for name, _, _ in REFUSED_CREATES]
)
def test_unsafe_creates_are_refused(name: str, body: dict[str, object], word: str) -> None:
    allowed, reason = create(body)
    assert not allowed, name
    assert word.lower() in reason.lower(), reason


@pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", b'"text"'])
def test_create_bodies_that_are_not_objects_are_refused(body: bytes) -> None:
    assert not check_request("POST", "/containers/create", body, CONFIG).allowed


def test_create_names_are_checked() -> None:
    assert create(sandbox_create(), "name=sandcage-run-1")[0]
    assert not create(sandbox_create(), "name=../x")[0]
    assert not create(sandbox_create(), "platform=linux/amd64")[0]


# --- exec and container actions ------------------------------------------------------


def exec_body(**changes: object) -> bytes:
    body: dict[str, object] = {
        "Cmd": ["sh", "-c", "ls"],
        "User": "65534:65534",
        "AttachStdout": True,
        "AttachStderr": True,
    }
    body.update(changes)
    return json.dumps({k: v for k, v in body.items() if v is not ...}).encode()


@pytest.mark.parametrize(
    ("body", "word"),
    [
        (exec_body(User="root"), "sandbox user"),
        (exec_body(User="0"), "sandbox user"),
        (exec_body(User=...), "sandbox user"),
        (exec_body(Privileged=True), "Privileged"),
        (exec_body(Env=["TOKEN=x"]), "Env"),
        (exec_body(AttachStdin=True), "AttachStdin"),
        (exec_body(Tty=True), "Tty"),
        (exec_body(WorkingDir="/"), "WorkingDir"),
        (exec_body(Cmd=[]), "command"),
        (exec_body(Cmd="ls"), "command"),
        (exec_body(DetachKeys="ctrl-p"), "DetachKeys"),
        (b"{bad", "JSON"),
    ],
)
def test_unsafe_execs_are_refused(body: bytes, word: str) -> None:
    decision = check_request("POST", "/containers/abc/exec", body, CONFIG)
    assert not decision.allowed
    assert word.lower() in decision.reason.lower(), decision.reason


@pytest.mark.parametrize(
    ("method", "target", "body", "word"),
    [
        ("DELETE", "/containers/abc?link=1", b"", "links"),
        ("GET", "/containers/abc/logs?follow=1", b"", "following"),
        ("POST", "/exec/e1/start", b'{"Detach": false, "Tty": true}', "Tty"),
        ("POST", "/exec/e1/start", b'{"ConsoleSize": [10, 10]}', "ConsoleSize"),
        ("GET", "/containers/json", b"", "filtered"),
        ("GET", '/containers/json?filters={"label":["app=x"]}', b"", "filtered"),
        ("GET", "/containers/json?all=1&size=1", b"", "size"),
        ("POST", "/containers/abc/start?detachKeys=x", b"", "detachKeys"),
        ("GET", "/containers/json?all=1&all=0", b"", "repeated"),
    ],
)
def test_risky_variants_of_allowed_calls_are_refused(
    method: str, target: str, body: bytes, word: str
) -> None:
    decision = check_request(method, target, body, CONFIG)
    assert not decision.allowed
    assert word.lower() in decision.reason.lower(), decision.reason


# --- everything else is refused: a fuzz list of API paths ----------------------------

REFUSED_CALLS = [
    ("POST", "/build"),
    ("POST", "/v1.44/build?t=x"),
    ("POST", "/build/prune"),
    ("POST", "/images/create?fromImage=alpine"),
    ("POST", "/images/load"),
    ("GET", "/images/get"),
    ("POST", "/images/sandcage-sandbox:latest/push"),
    ("POST", "/images/sandcage-sandbox:latest/tag?repo=x"),
    ("DELETE", "/images/sandcage-sandbox:latest"),
    ("GET", "/images/json"),
    ("POST", "/images/prune"),
    ("GET", "/images/search?term=x"),
    ("POST", "/commit?container=abc"),
    ("GET", "/volumes"),
    ("POST", "/volumes/create"),
    ("DELETE", "/volumes/x"),
    ("POST", "/volumes/prune"),
    ("GET", "/networks"),
    ("POST", "/networks/create"),
    ("POST", "/networks/bridge/connect"),
    ("DELETE", "/networks/x"),
    ("GET", "/containers/abc/archive?path=/"),
    ("PUT", "/containers/abc/archive?path=/"),
    ("HEAD", "/containers/abc/archive?path=/"),
    ("GET", "/containers/abc/export"),
    ("POST", "/containers/abc/attach?stream=1"),
    ("GET", "/containers/abc/attach/ws"),
    ("POST", "/containers/abc/update"),
    ("POST", "/containers/abc/rename?name=x"),
    ("POST", "/containers/abc/pause"),
    ("POST", "/containers/abc/unpause"),
    ("POST", "/containers/abc/restart"),
    ("POST", "/containers/abc/resize?h=1&w=1"),
    ("GET", "/containers/abc/top"),
    ("GET", "/containers/abc/changes"),
    ("GET", "/containers/abc/stats"),
    ("POST", "/containers/prune"),
    ("POST", "/exec/e1/resize?h=1&w=1"),
    ("GET", "/swarm"),
    ("POST", "/swarm/init"),
    ("GET", "/services"),
    ("POST", "/services/create"),
    ("GET", "/tasks"),
    ("GET", "/secrets"),
    ("POST", "/secrets/create"),
    ("GET", "/configs"),
    ("GET", "/nodes"),
    ("GET", "/plugins"),
    ("POST", "/plugins/pull?remote=x"),
    ("GET", "/system/df"),
    ("GET", "/events"),
    ("POST", "/auth"),
    ("POST", "/session"),
    ("GET", "/distribution/alpine/json"),
    ("POST", "/grpc"),
    ("GET", "/"),
    ("GET", ""),
    ("GET", "/v1.44"),
    ("PUT", "/_ping"),
    ("POST", "/info"),
    ("OPTIONS", "/containers/json"),
    ("GET", "/containers/abc/json/../../build"),
    ("GET", "/containers/./abc/json"),
    ("GET", "/containers//json"),
    ("GET", "/containers/abc%2fjson"),
    ("POST", "/containers%2Fcreate"),
    ("GET", "/containers\\abc\\json"),
    ("GET", "/containers/abc/json#x"),
    ("GET", "/containers/ab c/json"),
    ("GET", "/v1.44/v1.44/_ping"),
    ("GET", "/V1.44/_ping"),
    ("GET", "/containers/-abc/json"),
    ("GET", "/images/../etc/json"),
]


@pytest.mark.parametrize(("method", "target"), REFUSED_CALLS)
def test_every_other_api_call_is_refused(method: str, target: str) -> None:
    assert not check_request(method, target, b"{}", CONFIG).allowed


@given(st.sampled_from(["GET", "POST", "DELETE", "PUT", "HEAD"]), st.text(max_size=80))
def test_check_request_never_raises(method: str, target: str) -> None:
    decision = check_request(method, target, b"{}", CONFIG)
    assert isinstance(decision.allowed, bool)


@given(st.text(alphabet="/abcv1.4%_?=&-:", max_size=40))
def test_random_paths_only_reach_known_routes(target: str) -> None:
    decision = check_request("POST", target, b"{}", CONFIG)
    if decision.allowed:
        assert decision.container is not None or decision.exec_id is not None


# Written independently of the proxy's route table: every (method, path) the proxy may ever
# allow, with the API version prefix removed. Anything else allowed is a hole.
_ID_RE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
MAY_ALLOW = [
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"/_ping"),
        ("HEAD", r"/_ping"),
        ("GET", r"/version"),
        ("GET", r"/info"),
        ("GET", r"/images/[a-z0-9][a-z0-9._/:@-]*/json"),
        ("GET", r"/containers/json"),
        ("POST", r"/containers/create"),
        ("GET", rf"/containers/{_ID_RE}/(json|logs)"),
        ("POST", rf"/containers/{_ID_RE}/(start|stop|kill|wait|exec)"),
        ("DELETE", rf"/containers/{_ID_RE}"),
        ("POST", rf"/exec/{_ID_RE}/start"),
        ("GET", rf"/exec/{_ID_RE}/json"),
    )
]
DOCKER_SEGMENTS = [
    "containers",
    "exec",
    "images",
    "json",
    "create",
    "start",
    "stop",
    "kill",
    "wait",
    "logs",
    "_ping",
    "version",
    "info",
    "build",
    "volumes",
    "networks",
    "archive",
    "attach",
    "commit",
    "export",
    "update",
    "pause",
    "resize",
    "system",
    "df",
    "plugins",
    "secrets",
    "swarm",
    "auth",
    "session",
    "events",
    "prune",
    "push",
    "tag",
    "load",
    "get",
    "top",
    "sandcage-1",
    "abc123",
    "v1.44",
    "..",
    ".",
    "",
    "%2e%2e",
    "sandcage-sandbox:latest",
]


@given(
    st.sampled_from(["GET", "POST", "DELETE", "PUT", "HEAD"]),
    st.booleans(),
    st.lists(st.sampled_from(DOCKER_SEGMENTS), min_size=1, max_size=5),
    st.sampled_from(["", "?all=1", "?force=1", "?name=x", "?filters=%7B%7D", "?a=1&a=2"]),
)
def test_random_docker_paths_never_reach_an_unlisted_call(
    method: str, versioned: bool, segments: list[str], query: str
) -> None:
    path = ("/v1.44" if versioned else "") + "/" + "/".join(segments)
    decision = check_request(method, path + query, b"{}", CONFIG)
    if decision.allowed:
        bare = path.removeprefix("/v1.44") if versioned else path
        assert any(m == method and p.fullmatch(bare) for m, p in MAY_ALLOW), (method, path)


# --- configuration -------------------------------------------------------------------


def test_config_from_the_environment_requires_an_absolute_work_dir() -> None:
    assert ProxyConfig.from_env({"SANDCAGE_WORK_DIR": WORK}) == CONFIG
    assert ProxyConfig.from_env({"SANDCAGE_WORK_DIR": WORK, "SANDBOX_IMAGE": "x:1"}).image == "x:1"
    for bad in ("", "relative/dir", "/srv/../etc", "/srv/work/", "/"):
        with pytest.raises(ProxyConfigError):
            ProxyConfig.from_env({"SANDCAGE_WORK_DIR": bad})


def test_is_empty() -> None:
    for empty in (None, False, 0, 0.0, "", [], {}, [0, 0], {"Name": "", "Count": 0}):
        assert is_empty(empty), empty
    full: tuple[object, ...] = (True, 1, "x", [1], {"a": True}, {"bridge": {}}, {"/data": {}})
    for value in full:
        assert not is_empty(value), value
