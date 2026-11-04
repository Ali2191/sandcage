"""A real API server on 127.0.0.1 over the real store (scripted models, local runner), and
a small client that talks to it like the panel does: JSON calls with the access code and
an SSE reader with ``Last-Event-ID``."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from sandcage.api.app import App
from sandcage.api.server import Server, make_server
from sandcage.api.settings import ApiSettings, DemoSettings
from sandcage.sandbox.run import RunnerFactory
from tests.agent_helpers import LocalRunner
from tests.api_helpers import WAIT, Env, make_env
from tests.policy_helpers import engine

CODE = "judges-only-code-2026"


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class SseEvent:
    id: int
    kind: str
    data: dict[str, Any]


@dataclass
class Client:
    port: int
    code: str | None = CODE
    origin: str | None = None

    def call(
        self,
        method: str,
        path: str,
        body: JsonValue | bytes | None = None,
        *,
        headers: dict[str, str] | None = None,
        code: str | None = "default",
    ) -> Reply:
        sent = dict(headers or {})
        use = self.code if code == "default" else code
        if use is not None:
            sent["X-Access-Code"] = use
        if self.origin:
            sent["Origin"] = self.origin
        data = body if isinstance(body, bytes) else None
        if body is not None and not isinstance(body, bytes):
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=WAIT)
        try:
            conn.request(method, path, body=data, headers=sent)
            reply = conn.getresponse()
            return Reply(reply.status, {k.lower(): v for k, v in reply.getheaders()}, reply.read())
        finally:
            conn.close()

    @contextmanager
    def stream(self, run_id: str, last_id: int | None = None) -> Iterator[Iterator[SseEvent | str]]:
        """Open the event stream; yields events and ``": ping"`` comments as they come."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=WAIT)
        headers = {"Last-Event-ID": str(last_id)} if last_id is not None else {}
        conn.request("GET", f"/runs/{run_id}/events", headers=headers)
        reply = conn.getresponse()
        assert reply.status == 200, reply.read()
        assert reply.getheader("Content-Type", "").startswith("text/event-stream")

        def read() -> Iterator[SseEvent | str]:
            fields: dict[str, str] = {}
            while True:
                raw = reply.fp.readline()
                if not raw:
                    return
                line = raw.decode().rstrip("\n")
                if line.startswith(":"):
                    yield line
                    continue
                if line == "":
                    if "data" in fields:
                        yield SseEvent(
                            int(fields["id"]), fields["event"], json.loads(fields["data"])
                        )
                    fields = {}
                    continue
                name, _, value = line.partition(": ")
                fields[name] = value

        try:
            yield read()
        finally:
            conn.close()


@dataclass
class Served:
    env: Env
    app: App
    server: Server
    client: Client
    threads: list[threading.Thread] = field(default_factory=list)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])


@contextmanager
def serve(
    tmp: Path,
    *,
    demo: DemoSettings | None = None,
    panel_origin: str | None = None,
    heartbeat: float = 15.0,
    approval_timeout: float | None = None,
    runner_factory: RunnerFactory = LocalRunner,
    readiness: Callable[[], dict[str, dict[str, JsonValue]]] | None = None,
) -> Iterator[Served]:
    demo = demo or DemoSettings(access_code=CODE, runs_per_client_per_hour=100, runs_per_day=100)
    env = make_env(tmp, demo=demo, approval_timeout=approval_timeout, runner_factory=runner_factory)
    settings = ApiSettings(demo=demo, panel_origin=panel_origin, data_dir=tmp / "data")
    app = App(
        settings=settings,
        store=env.store,
        guard=env.guard,
        manager=env.manager,
        engine=engine(),
        readiness=readiness
        or (
            lambda: {
                "docker": {"ok": True, "detail": "fake"},
                "runtime": {"ok": True, "detail": "fake"},
            }
        ),
        runtime=lambda: {"active": "runc", "label": "runc (plain Docker)", "gvisor": False},
        bench_results_path=tmp / "results.json",
        heartbeat=heartbeat,
    )
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    served = Served(env, app, server, Client(int(server.server_address[1])))
    try:
        yield served
    finally:
        for run_id in env.manager.active_ids():
            env.manager.stop(run_id)
            env.manager.wait(run_id, WAIT)
        server.shutdown()
        server.server_close()
        thread.join(WAIT)


def answer_all(
    client: Client,
    run_id: str,
    *,
    choose: Callable[[dict[str, Any]], tuple[str, str | None] | None] | None = None,
) -> list[SseEvent]:
    """Stream a run to its end over HTTP, answering every approval like the panel."""
    seen: list[SseEvent] = []
    with client.stream(run_id) as events:
        for item in events:
            if isinstance(item, str):
                continue
            seen.append(item)
            if item.kind == "approval_needed":
                approval = item.data["approval"]
                picked = (choose or approve_choice)(approval)
                if picked is not None:
                    choice, answer = picked
                    body: dict[str, JsonValue] = {"approval_id": approval["id"], "choice": choice}
                    if answer is not None:
                        body["answer"] = answer
                    reply = client.call("POST", f"/runs/{run_id}/approve", body)
                    assert reply.status == 200, reply.body
    return seen


def approve_choice(approval: dict[str, Any]) -> tuple[str, str | None]:
    if approval.get("free_text"):
        return "answer", "A percentage: 10 means 10% off."
    return next(c["id"] for c in approval["choices"] if c["approves"]), None


def refuse_choice(approval: dict[str, Any]) -> tuple[str, str | None]:
    return next(c["id"] for c in approval["choices"] if not c["approves"]), None
