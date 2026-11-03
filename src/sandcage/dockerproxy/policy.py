"""The deny-by-default allowlist of Docker Engine API calls (pure logic, no I/O).

Allowed:

- harmless reads: ping, version, info, image inspection, and the container list only
  when it is filtered to sandbox-labelled containers;
- container creation only for a hardened sandbox: the sandbox image, the sandbox label,
  the unprivileged user, no environment, no network, read-only root, every capability
  dropped, no new privileges and no other security options, memory up to 2 GiB with no
  extra swap, PID and CPU limits, the default or gVisor runtime, only a noexec /tmp
  tmpfs, at most one bind mount (a normalised path inside the work folder mapped to
  /workspace), and every other field empty;
- inspect, start, stop, kill, wait, logs, remove and exec on a container, and start and
  inspect of an exec, only after the server has confirmed upstream that the container
  carries the sandbox label (the decision names what to check); exec only as the sandbox
  user, never privileged, never with environment variables; removal never of links.

Everything else is refused: builds, pulls, volumes, networks, file copies, attach,
commit, swarm, plugins, system calls, other images, and any path with encoding,
backslashes, doubled slashes or dot segments. A ``/vN.NN`` version prefix is stripped.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qs

SANDBOX_LABEL: Final = ("sandcage", "sandbox")
SANDBOX_USERS: Final = frozenset({"65534:65534", "65534"})
WORKSPACE: Final = "/workspace"
MAX_MEMORY: Final = 2 * 1024 * 1024 * 1024
MAX_PIDS: Final = 1024
MAX_NANO_CPUS: Final = 4_000_000_000
RUNTIMES: Final = frozenset({"", "runc", "runsc"})
NO_NEW_PRIVILEGES: Final = frozenset(
    {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
)
_VERSION: Final = re.compile(r"/v[0-9]{1,2}\.[0-9]{1,3}(?=/)")
_ID: Final = r"(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})"
_IMAGE: Final = r"(?P<image>[a-z0-9][a-z0-9._/:@-]{0,254})"
_TMPFS_TOKEN: Final = re.compile(r"rw|noexec|nosuid|nodev|size=[0-9]{1,6}[kmg]?|mode=1777")
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class ProxyConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProxyConfig:
    image: str
    work_dir: str

    def __post_init__(self) -> None:
        if not self.work_dir.startswith("/") or posixpath.normpath(self.work_dir) != self.work_dir:
            raise ProxyConfigError("SANDCAGE_WORK_DIR must be an absolute, normalised path")
        if self.work_dir == "/":
            raise ProxyConfigError("SANDCAGE_WORK_DIR may not be the root directory")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProxyConfig:
        env = os.environ if environ is None else environ
        work_dir = env.get("SANDCAGE_WORK_DIR", "")
        if not work_dir:
            raise ProxyConfigError("set SANDCAGE_WORK_DIR (where sandbox workspaces live)")
        return cls(env.get("SANDBOX_IMAGE") or "sandcage-sandbox:latest", work_dir)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    path: str = ""  # the normalised path, without the version prefix
    container: str | None = None  # confirm upstream that this container is a sandbox
    exec_id: str | None = None  # confirm upstream that this exec's container is a sandbox


def refuse(reason: str) -> Decision:
    return Decision(False, reason)


# --- the entry point -----------------------------------------------------------------


def check_request(method: str, target: str, body: bytes, config: ProxyConfig) -> Decision:
    """The verdict on one HTTP request to the Docker API."""
    raw_path, _, raw_query = target.partition("?")
    problem = _path_problem(raw_path)
    if problem is not None:
        return refuse(problem)
    version = _VERSION.match(raw_path)
    path = raw_path[version.end() :] if version else raw_path
    try:
        query = _query(raw_query)
    except ValueError as error:
        return refuse(str(error))
    for route_method, pattern, keys, handler in _ROUTES:
        match = pattern.fullmatch(path)
        if match is None or method != route_method:
            continue
        unexpected = set(query) - keys
        if unexpected:
            return refuse(f"query parameter {sorted(unexpected)[0]!r} is not allowed here")
        verdict = handler(match.groupdict(), query, body, config)
        return Decision(verdict.allowed, verdict.reason, path, verdict.container, verdict.exec_id)
    return refuse(f"{method} {path} is not an allowed Docker API call")


def _path_problem(path: str) -> str | None:
    if not path.startswith("/"):
        return "the path must start with /"
    if any(ch in path for ch in "%\\#") or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in path):
        return "the path contains encoded or unusual characters"
    if "//" in path:
        return "the path contains a doubled slash"
    if any(part in {".", ".."} for part in path.split("/")):
        return "the path contains a dot segment"
    return None


def _query(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("the query string is malformed") from None
    single: dict[str, str] = {}
    for key, values in parsed.items():
        if len(values) != 1:
            raise ValueError(f"query parameter {key!r} is repeated")
        single[key] = values[0]
    return single


# --- handlers ------------------------------------------------------------------------

Handler = Callable[[dict[str, str], dict[str, str], bytes, ProxyConfig], Decision]

ALLOW: Final = Decision(True, "allowed")


def _allow(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    return ALLOW


def _image_inspect(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    return ALLOW if ".." not in groups["image"] else refuse("the image name is malformed")


def _container_list(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    try:
        filters = json.loads(query.get("filters", ""))
    except ValueError:
        return refuse("the container list must be filtered to sandbox containers")
    labels = filters.get("label") if isinstance(filters, dict) else None
    if not isinstance(labels, list) or "=".join(SANDBOX_LABEL) not in labels:
        return refuse("the container list must be filtered to sandbox containers")
    if query.get("all", "1") not in {"0", "1", "true", "false"}:
        return refuse("the 'all' parameter is malformed")
    return ALLOW


def _sandbox_container(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    return Decision(True, "allowed for a sandbox container", container=groups["id"])


def _logs(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    if query.get("follow", "0") not in {"0", "false"}:
        return refuse("following logs is not allowed")
    return _sandbox_container(groups, query, body, config)


def _remove(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    if query.get("link", "0") not in {"0", "false"}:
        return refuse("removing links is not allowed")
    return _sandbox_container(groups, query, body, config)


def _exec_of_sandbox(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    return Decision(True, "allowed for an exec in a sandbox", exec_id=groups["id"])


def _create(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    name = query.get("name")
    if name is not None and not _NAME.fullmatch(name):
        return refuse("the container name is malformed")
    request = _json_object(body)
    if request is None:
        return refuse("the create request is not a JSON object")
    problem = check_create(request, config)
    return refuse(problem) if problem else ALLOW


def _exec_create(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    request = _json_object(body)
    if request is None:
        return refuse("the exec request is not a JSON object")
    problem = check_exec(request)
    if problem:
        return refuse(problem)
    return _sandbox_container(groups, query, body, config)


def _exec_start(
    groups: dict[str, str], query: dict[str, str], body: bytes, config: ProxyConfig
) -> Decision:
    request = _json_object(body) if body else {}
    if request is None:
        return refuse("the exec start request is not a JSON object")
    problem = _only(request, {"Detach": _is_bool, "Tty": _is_false})
    if problem:
        return refuse(problem)
    return _exec_of_sandbox(groups, query, body, config)


def _route(
    method: str, path: str, keys: frozenset[str], handler: Handler
) -> tuple[str, re.Pattern[str], frozenset[str], Handler]:
    return method, re.compile(path), keys, handler


_NONE: Final = frozenset[str]()
_ROUTES: Final = (
    _route("GET", "/_ping", _NONE, _allow),
    _route("HEAD", "/_ping", _NONE, _allow),
    _route("GET", "/version", _NONE, _allow),
    _route("GET", "/info", _NONE, _allow),
    _route("GET", f"/images/{_IMAGE}/json", _NONE, _image_inspect),
    _route("GET", "/containers/json", frozenset({"all", "filters"}), _container_list),
    _route("POST", "/containers/create", frozenset({"name"}), _create),
    _route("GET", f"/containers/{_ID}/json", _NONE, _sandbox_container),
    _route("POST", f"/containers/{_ID}/start", _NONE, _sandbox_container),
    _route("POST", f"/containers/{_ID}/stop", frozenset({"t"}), _sandbox_container),
    _route("POST", f"/containers/{_ID}/kill", frozenset({"signal"}), _sandbox_container),
    _route("POST", f"/containers/{_ID}/wait", frozenset({"condition"}), _sandbox_container),
    _route(
        "GET",
        f"/containers/{_ID}/logs",
        frozenset({"stdout", "stderr", "follow", "tail", "timestamps"}),
        _logs,
    ),
    _route("DELETE", f"/containers/{_ID}", frozenset({"force", "v", "link"}), _remove),
    _route("POST", f"/containers/{_ID}/exec", _NONE, _exec_create),
    _route("POST", f"/exec/{_ID}/start", _NONE, _exec_start),
    _route("GET", f"/exec/{_ID}/json", _NONE, _exec_of_sandbox),
)


# --- body checks ---------------------------------------------------------------------

Check = Callable[[object], bool]


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def is_empty(value: object) -> bool:
    """None, False, 0, "", lists of empty values, and dicts with no keys or only empty
    scalar or list values. A dict holding a dict is never empty, because its keys carry
    meaning (``{"/data": {}}`` is an anonymous volume, ``{"bridge": {}}`` a network)."""
    if value is None or value is False or value == "":
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, list | tuple):
        return all(is_empty(item) for item in value)
    if isinstance(value, dict):
        return all(not isinstance(item, dict) and is_empty(item) for item in value.values())
    return False


def _only(request: Mapping[str, object], checks: Mapping[str, Check]) -> str | None:
    """Every key with a check must pass it (when present and non-empty, or when the check
    demands presence); every other key must be empty."""
    for key, value in request.items():
        check = checks.get(key)
        if check is None:
            if not is_empty(value):
                return f"the field {key} is not allowed"
        elif not check(value):
            return f"the field {key} has a value that is not allowed"
    return None


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_false(value: object) -> bool:
    return value is False or value is None


def _is_true(value: object) -> bool:
    return value is True


def _is_command(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and "\x00" not in item for item in value)
    )


def _int_between(low: int, high: int) -> Check:
    def check(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

    return check


def check_create(request: Mapping[str, object], config: ProxyConfig) -> str | None:
    """None when the create request is exactly a hardened sandbox, else the problem."""
    required = {
        "Image": "the sandbox image",
        "User": "the sandbox user",
        "Labels": "the sandbox label",
        "NetworkDisabled": "no network",
        "HostConfig": "the hardened host settings",
    }
    for key, what in required.items():
        if key not in request:
            return f"a sandbox needs {what} ({key})"
    problem = _only(
        request,
        {
            "Image": lambda value: value == config.image,
            "Cmd": _is_command,
            "User": lambda value: value in SANDBOX_USERS,
            "WorkingDir": lambda value: value in {"", WORKSPACE},
            "Labels": _sandbox_labels,
            "NetworkDisabled": _is_true,
            "Env": is_empty,
            "AttachStdin": _is_false,
            "AttachStdout": _is_bool,
            "AttachStderr": _is_bool,
            "Tty": _is_false,
            "OpenStdin": _is_false,
            "HostConfig": lambda value: isinstance(value, dict),
        },
    )
    if problem:
        if "Image" in problem:
            return "only the sandbox image may be run"
        return problem
    host = request["HostConfig"]
    if not isinstance(host, dict):
        return "the field HostConfig has a value that is not allowed"
    return check_host_config(host, config)


def _sandbox_labels(value: object) -> bool:
    if not isinstance(value, dict) or value.get(SANDBOX_LABEL[0]) != SANDBOX_LABEL[1]:
        return False
    return all(
        isinstance(key, str)
        and isinstance(item, str)
        and (key == SANDBOX_LABEL[0] or key.startswith(SANDBOX_LABEL[0] + "."))
        for key, item in value.items()
    )


def check_host_config(host: Mapping[str, object], config: ProxyConfig) -> str | None:
    must = {
        "NetworkMode": "no network",
        "ReadonlyRootfs": "a read-only root",
        "CapDrop": "every capability dropped",
        "SecurityOpt": "no new privileges",
        "PidsLimit": "a process limit",
        "Memory": "a memory limit",
        "NanoCpus": "a CPU limit",
    }
    for key, what in must.items():
        if is_empty(host.get(key)):
            return f"a sandbox needs {what} ({key})"
    memory = host.get("Memory")
    return _only(
        host,
        {
            "NetworkMode": lambda value: value == "none",
            "ReadonlyRootfs": _is_true,
            "CapDrop": lambda value: (
                isinstance(value, list) and [str(item).upper() for item in value] == ["ALL"]
            ),
            "SecurityOpt": lambda value: (
                isinstance(value, list) and len(value) == 1 and value[0] in NO_NEW_PRIVILEGES
            ),
            "PidsLimit": _int_between(1, MAX_PIDS),
            "Memory": _int_between(1, MAX_MEMORY),
            "MemorySwap": lambda value: value == memory,
            "NanoCpus": _int_between(1, MAX_NANO_CPUS),
            "Runtime": lambda value: value is None or value in RUNTIMES,
            "Tmpfs": _tmpfs,
            "Binds": lambda value: _binds(value, config),
            "Privileged": _is_false,
        },
    )


def _tmpfs(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"/tmp"}:  # noqa: S108 - the container's
        return False
    options = value["/tmp"]  # noqa: S108
    if not isinstance(options, str):
        return False
    tokens = options.split(",")
    return (
        all(_TMPFS_TOKEN.fullmatch(token) for token in tokens)
        and "noexec" in tokens
        and "nosuid" in tokens
    )


def _binds(value: object, config: ProxyConfig) -> bool:
    if not isinstance(value, list) or len(value) > 1:
        return False
    for bind in value:
        if not isinstance(bind, str) or "\x00" in bind or "\n" in bind:
            return False
        parts = bind.split(":")
        if len(parts) == 3 and parts[2] == "rw":
            parts = parts[:2]
        if len(parts) != 2 or parts[1] != WORKSPACE:
            return False
        source = parts[0]
        if not source.startswith("/") or posixpath.normpath(source) != source:
            return False
        if source == config.work_dir or not source.startswith(config.work_dir + "/"):
            return False
    return True


def check_exec(request: Mapping[str, object]) -> str | None:
    """None when the exec runs a command as the sandbox user with nothing extra."""
    if request.get("User") not in SANDBOX_USERS:
        return "exec is allowed only as the sandbox user"
    if not _is_command(request.get("Cmd")):
        return "exec needs a command"
    return _only(
        request,
        {
            "Cmd": _is_command,
            "User": lambda value: value in SANDBOX_USERS,
            "AttachStdin": _is_false,
            "AttachStdout": _is_bool,
            "AttachStderr": _is_bool,
            "Tty": _is_false,
            "Privileged": _is_false,
            "Env": is_empty,
            "WorkingDir": lambda value: value in {"", WORKSPACE},
        },
    )
