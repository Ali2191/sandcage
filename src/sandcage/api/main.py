"""Startup: build the API from the environment and serve it.

Order (Specs/API.md "Startup behaviour"):

1. Read the settings. A malformed or insecure setting (public mode without a 12+
   character code) stops here, naming the setting only. Warn when no code is set.
2. Take the event log's lock file: exactly one API process per log, or refuse to start.
3. Open the event store (migrating it), then mark runs a previous process left
   ``running`` as interrupted, so their streams end.
4. Probe the sandbox runtime once and log which one new runs get (never claims gVisor
   without its banner); remove sandboxes and workspaces left by dead owners. Docker
   being unreachable is logged, not fatal.
5. Serve.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pydantic import JsonValue

from sandcage.api.app import App, disk_check
from sandcage.api.catalog import demo_repos
from sandcage.api.guard import DemoGuard
from sandcage.api.runs import RunManager
from sandcage.api.server import Server, make_server
from sandcage.api.settings import ApiSettings, SettingsError
from sandcage.demo_repos import DEMO_REPOS_DIR
from sandcage.events.store import EventStore, StoreError
from sandcage.masking import mask
from sandcage.policy import PolicyEngine
from sandcage.sandbox.container import DockerSandbox, reap_orphaned_sandboxes
from sandcage.sandbox.docker_api import DockerAPI, DockerError, EngineClient
from sandcage.sandbox.run import RunnerFactory
from sandcage.sandbox.runtime import RuntimeConfigError, RuntimeSelector, requested_runtime

log = logging.getLogger("sandcage.api")


class StartupError(Exception):
    """The API must not start. The message is safe to print."""


@dataclass
class Built:
    app: App
    store: EventStore
    lock: IO[str]


def lock_database(path: Path) -> IO[str]:
    """Hold an exclusive lock next to the event log for the life of the process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = (path.parent / (path.name + ".lock")).open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise StartupError(
            "another API process is already using this event log; run exactly one per log"
        ) from None
    return handle


def docker_runner(api: DockerAPI, selector: RuntimeSelector) -> RunnerFactory:
    def factory(workspace: Path) -> DockerSandbox:
        return DockerSandbox(api, workspace, runtime=selector.status().active)

    return factory


def build(
    environ: Mapping[str, str] | None = None,
    *,
    api: DockerAPI | None = None,
    runner_factory: RunnerFactory | None = None,
) -> Built:
    env = dict(os.environ if environ is None else environ)
    try:
        settings = ApiSettings.from_env(env)
        requested = requested_runtime(env)
    except (SettingsError, RuntimeConfigError) as problem:
        raise StartupError(str(problem)) from None
    if settings.demo.access_code is None:
        log.warning("no DEMO_ACCESS_CODE is set: anyone who can reach the API can start runs")
    lock = lock_database(settings.db_path)
    try:
        store = EventStore(settings.db_path)
    except StoreError as problem:
        lock.close()
        raise StartupError(str(problem)) from None
    docker = api or EngineClient.from_env(env)
    selector = RuntimeSelector(docker, requested)
    status = selector.status()
    log.info("sandbox runtime: %s (%s)", status.label, status.reason)
    guard = DemoGuard(settings.demo, store)
    engine = PolicyEngine.from_file()
    temp_root = Path(env.get("TMPDIR") or tempfile.gettempdir())
    manager = RunManager(
        store=store,
        guard=guard,
        repos=demo_repos(DEMO_REPOS_DIR),
        runner_factory=runner_factory or docker_runner(docker, selector),
        provider=settings.provider,
        learned_path=settings.learned_path,
        runtime_label=lambda: selector.status().label,
    )
    recovered = manager.recover()
    if recovered:
        log.info("marked %d interrupted run(s) from a previous process", len(recovered))
    try:
        reaped = reap_orphaned_sandboxes(docker)
        if reaped.containers:
            log.info("removed %d orphaned sandbox(es)", len(reaped.containers))
    except DockerError as problem:
        log.warning("could not reap orphaned sandboxes: %s", mask(str(problem))[:200])

    def runtime() -> dict[str, JsonValue]:
        found = selector.status().to_dict()
        return {
            key: value
            for key, value in found.items()
            if value is None or isinstance(value, str | bool)
        }

    app = App(
        settings=settings,
        store=store,
        guard=guard,
        manager=manager,
        engine=engine,
        readiness=readiness(docker, selector, temp_root),
        runtime=runtime,
        bench_results_path=Path(
            env.get("SANDCAGE_BENCH_RESULTS", "") or "benchmarks/out/results.json"
        ),
    )
    return Built(app, store, lock)


def readiness(
    docker: DockerAPI, selector: RuntimeSelector, temp_root: Path
) -> Callable[[], dict[str, dict[str, JsonValue]]]:
    def checks() -> dict[str, dict[str, JsonValue]]:
        try:
            docker.ping()
            reachable: dict[str, JsonValue] = {"ok": True, "detail": "Docker answers"}
        except DockerError:
            reachable = {"ok": False, "detail": "Docker is unreachable"}
        status = selector.status()
        return {
            "docker": reachable,
            "runtime": {
                "ok": status.active is not None,
                "detail": status.reason,
                "active": status.active,
                "label": status.label,
                "gvisor": status.gvisor,
            },
            "disk": disk_check(temp_root),
        }

    return checks


def serve(built: Built, host: str, port: int) -> Server:
    server = make_server(built.app, host, port)
    log.info("listening on %s:%d", host, server.server_address[1])
    return server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        built = build()
    except StartupError as problem:
        print(f"sandcage api: not starting: {problem}", file=sys.stderr)
        return 2
    settings = built.app.settings
    server = serve(built, settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        built.store.close()
        built.lock.close()
    return 0
