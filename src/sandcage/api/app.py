"""The API's routes, independent of the HTTP server: a :class:`Request` in, a
:class:`Response` (or an :class:`EventStream`) out.

- Every route is in :attr:`App.routes`; a route that changes state is marked
  ``changes=True`` and always passes the demo guard's access-code check (a test
  enumerates the table, so a new changing route without the check cannot slip in).
- ``POST /runs`` checks the kill switch first, then the code.
- Run ids must be 12 hex characters before anything looks them up; repos and Bench
  scenarios are exact names. No route accepts a path or a URL to run against.
- Bodies are validated strictly (unknown fields refused, sizes capped).
- Errors are ``{"detail": "..."}`` with the status of the :class:`ApiError` (or the guard's
  refusal) and ``Retry-After`` where it helps.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Final, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
)

from sandcage.api import catalog
from sandcage.api.errors import (
    ApiError,
    BadRequestError,
    ConflictError,
    InvalidError,
    LockedError,
    NotFoundError,
)
from sandcage.api.guard import DemoGuard, RefusalError
from sandcage.api.runs import RunManager, StartRequest
from sandcage.api.settings import HEARTBEAT_SECONDS, ApiSettings
from sandcage.contract.events import (
    DecisionCounts,
    DoneEvent,
    RunStatus,
    StepEvent,
    UnknownEvent,
    dump_event,
)
from sandcage.events.store import EventStore, Outputs, RunRecord
from sandcage.policy import Decision, PolicyEngine

RUN_ID_PART: Final = r"(?P<run_id>[0-9a-f]{12})"
TASK_LIMIT: Final = 2000
CRITERIA_LIMIT: Final = 8
MIN_FREE_BYTES: Final = 1024**3

Readiness = Callable[[], dict[str, dict[str, JsonValue]]]


# --- requests and responses -------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""
    peer: str = "127.0.0.1"

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data: JsonValue | Mapping[str, object], status: int = 200) -> Response:
        return cls(status, json.dumps(data, ensure_ascii=False).encode())

    @classmethod
    def text(
        cls, text: str, content_type: str, *, headers: Mapping[str, str] | None = None
    ) -> Response:
        return cls(200, text.encode(), content_type, headers or {})

    @classmethod
    def error(cls, status: int, message: str, retry_after: int | None = None) -> Response:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        return cls(status, json.dumps({"detail": message}).encode(), headers=headers)


@dataclass(frozen=True)
class EventStream:
    """Tells the server to stream a run's events (``text/event-stream``)."""

    run_id: str
    after_id: int


Handler = Callable[[Request, Mapping[str, str]], Response | EventStream]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    changes: bool  # needs the access code


# --- request bodies ------------------------------------------------------------------------------

_Short = Annotated[StrictStr, Field(min_length=1, max_length=100)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_B = TypeVar("_B", bound=_Body)


class StartBody(_Body):
    repo: _Short | None = None
    scenario: _Short | None = None
    task: Annotated[StrictStr, Field(max_length=TASK_LIMIT)] | None = None
    guard: StrictBool = True
    approve_plan: StrictBool = False
    done_criteria: Annotated[
        list[Annotated[StrictStr, Field(min_length=1, max_length=200)]],
        Field(max_length=CRITERIA_LIMIT),
    ] = Field(default_factory=list)
    fake_scenario: _Short | None = None


class ApproveBody(_Body):
    approval_id: _Short
    choice: _Short
    answer: Annotated[StrictStr, Field(max_length=2000)] | None = None


class RewindBody(_Body):
    checkpoint: Annotated[StrictInt, Field(ge=0, le=10_000)]


class AcceptBody(_Body):
    suggestion: Annotated[StrictStr, Field(min_length=1, max_length=200)]


def _parse(kind: type[_B], body: bytes) -> _B:
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise BadRequestError("The request body is not valid JSON.") from None
    try:
        return kind.model_validate(data)
    except ValidationError as problem:
        first = problem.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "body"
        raise InvalidError(f"{where}: {first['msg']}") from None


# --- the app ------------------------------------------------------------------------------------


class App:
    def __init__(
        self,
        *,
        settings: ApiSettings,
        store: EventStore,
        guard: DemoGuard,
        manager: RunManager,
        engine: PolicyEngine,
        readiness: Readiness,
        runtime: Callable[[], dict[str, JsonValue] | None] = lambda: None,
        bench_results_path: Path = Path("benchmarks/out/results.json"),
        heartbeat: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.guard = guard
        self.manager = manager
        self.engine = engine
        self._readiness = readiness
        self._runtime = runtime
        self._bench_results = bench_results_path
        self.heartbeat = heartbeat
        self._clock = clock
        self.routes = self._routes()

    def _routes(self) -> list[Route]:
        def route(method: str, path: str, handler: Handler) -> Route:
            pattern = re.compile(path.replace("{id}", RUN_ID_PART))
            return Route(method, pattern, handler, changes=method != "GET")

        run = "/runs/{id}"
        return [
            route("GET", "/healthz", lambda r, m: Response.json({"ok": True})),
            route("GET", "/readyz", self.readyz),
            route("GET", "/demo/status", self.demo_status),
            route("POST", "/demo/unlock", lambda r, m: Response.json({"ok": True})),
            route("GET", "/repos", lambda r, m: Response.json({"repos": self.manager.repos})),
            route("GET", "/bench/scenarios", lambda r, m: Response.json(catalog.bench_scenarios())),
            route("GET", "/bench/results", self.bench_results),
            route("GET", "/policy", lambda r, m: Response.json(catalog.policy(self.engine))),
            route("POST", "/runs", self.start),
            route("GET", "/runs", self.list_runs),
            route("GET", run, self.get_run),
            route("POST", run + "/stop", self.stop),
            route("POST", run + "/approve", self.approve),
            route("GET", run + "/checkpoints", self.checkpoints),
            route("POST", run + "/rewind", self.rewind),
            route("GET", run + "/patch", self.patch),
            route("GET", run + "/summary", self.summary),
            route("GET", run + "/report", self.report),
            route("POST", run + "/release", self.release),
            route("GET", run + "/learn", self.learn),
            route("POST", run + "/learn/accept", self.accept),
            route("GET", run + "/events", self.events),
        ]

    # --- dispatch ------------------------------------------------------------------------

    def handle(self, request: Request) -> Response | EventStream:
        path_matched = False
        for route in self.routes:
            found = route.pattern.fullmatch(request.path)
            if found is None:
                continue
            path_matched = True
            if route.method != request.method:
                continue
            try:
                if route.changes:
                    self._check(request, starting=route.handler == self.start)
                return route.handler(request, found.groupdict())
            except RefusalError as refused:
                return Response.error(refused.status, refused.message, refused.retry_after)
            except ApiError as problem:
                return Response.error(problem.status, problem.message, problem.retry_after)
        if path_matched:
            return Response.error(405, "That method is not allowed here.")
        return Response.error(404, "Not found.")

    def client(self, request: Request) -> str:
        return self.guard.client(request.peer, request.header("x-forwarded-for"))

    def _check(self, request: Request, *, starting: bool) -> None:
        if starting:
            self.guard.check_open()  # the kill switch answers before the code
        self.guard.check_code(self.client(request), request.header("x-access-code"))

    # --- health and demo -------------------------------------------------------------------

    def readyz(self, request: Request, match: Mapping[str, str]) -> Response:
        checks = self._readiness()
        provider = self.settings.provider
        checks["provider"] = {
            "ok": provider.configured,
            "detail": provider.problem or f"{provider.provider} provider configured",
        }
        ready = all(bool(check.get("ok")) for check in checks.values())
        return Response.json({"ready": ready, "checks": checks}, 200 if ready else 503)

    def demo_status(self, request: Request, match: Mapping[str, str]) -> Response:
        status = self.guard.status(active_runs=len(self.manager.active_ids()))
        status["sandbox_runtime"] = self._runtime()
        status["provider_ready"] = self.settings.provider.configured
        return Response.json(status)

    def bench_results(self, request: Request, match: Mapping[str, str]) -> Response:
        found = catalog.bench_results(self._bench_results)
        if found is None:
            return Response.error(404, "No Bench results yet. Run the Bench to produce them.")
        return Response.json(found)

    # --- runs ------------------------------------------------------------------------------

    def start(self, request: Request, match: Mapping[str, str]) -> Response:
        body = _parse(StartBody, request.body)
        if (body.repo is None) == (body.scenario is None):
            raise InvalidError("Give exactly one of repo and scenario.")
        task = (body.task or "").strip()
        if body.scenario is not None:
            found = catalog.bench_scenario(body.scenario)
            if found is None:
                raise NotFoundError("There is no Bench scenario by that name.")
            task = found.task  # a scenario brings its own task
        elif not task:
            raise InvalidError("task: a task of 1 to 2000 characters is needed")
        run_id = self.manager.start(
            StartRequest(
                repo=f"bench:{body.scenario}" if body.scenario is not None else (body.repo or ""),
                scenario=body.scenario,
                task=task,
                guard=body.guard,
                approve_plan=body.approve_plan,
                done_criteria=tuple(c.strip() for c in body.done_criteria if c.strip()),
                fake_scenario=body.fake_scenario,
            ),
            client=self.client(request),
        )
        return Response.json({"id": run_id, "run_id": run_id}, 202)

    def list_runs(self, request: Request, match: Mapping[str, str]) -> Response:
        limit = _int(request.query.get("limit"), 20, "limit", 1, 100)
        offset = _int(request.query.get("offset"), 0, "offset", 0, 10**9)
        records, total = self.store.list_runs(limit=limit, offset=offset)
        return Response.json(
            {
                "runs": [self.run_row(r) for r in records],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    def get_run(self, request: Request, match: Mapping[str, str]) -> Response:
        record = self.manager.run(match["run_id"])
        row = self.run_row(record)
        pending = self.manager.pending(record.id)
        row["pending"] = pending.model_dump(mode="json") if pending else None
        row["result"] = dump_event(record.done) if record.done else None
        row["server_time"] = self._clock()
        return Response.json(row)

    def run_row(self, record: RunRecord) -> dict[str, JsonValue]:
        done = record.done
        if done is not None:
            steps, counts, budget = done.steps, done.counts, done.budget
        else:
            events = self.store.events(record.id)
            step_events = [e for e in events if isinstance(e, StepEvent)]
            steps, counts = len(step_events), _counts(step_events)
            budgets = [e.budget for e in events if not isinstance(e, UnknownEvent) and e.budget]
            budget = budgets[-1] if budgets else None
        outputs = self.store.outputs(record.id)
        blocked = outputs.blocked if outputs else bool(done and done.delivery_blocked)
        # released during the run (the red-team question) or afterwards (POST release)
        released = record.released_by_human or bool(done and done.released_by_human)
        return {
            "id": record.id,
            "repo": record.repo,
            "task": record.task,
            "guard": record.guard,
            "status": record.status.value,
            "created": record.created,
            "finished": record.finished,
            "provider": record.provider,
            "model": record.model,
            "sandbox_runtime": done.sandbox_runtime if done else None,
            "steps": steps,
            "counts": counts.model_dump(mode="json"),
            "tokens": budget.tokens if budget else record.tokens,
            "tokens_reported": budget.tokens_reported if budget else False,
            "outcome": done.outcome if done else None,
            "delivered": bool(done and done.delivered) or record.released_by_human,
            "delivery_blocked": blocked,
            "released_by_human": released,
        }

    def stop(self, request: Request, match: Mapping[str, str]) -> Response:
        status = self.manager.stop(match["run_id"])
        value = status.value if isinstance(status, RunStatus) else status
        return Response.json({"id": match["run_id"], "status": value})

    def approve(self, request: Request, match: Mapping[str, str]) -> Response:
        body = _parse(ApproveBody, request.body)
        resolution = self.manager.approve(
            match["run_id"], body.approval_id, body.choice, body.answer
        )
        return Response.json(
            {
                "id": match["run_id"],
                "approval_id": body.approval_id,
                "choice": resolution.choice,
                "approved": resolution.approved,
            }
        )

    def checkpoints(self, request: Request, match: Mapping[str, str]) -> Response:
        listed = self.manager.checkpoints(match["run_id"])
        return Response.json({"checkpoints": listed})

    def rewind(self, request: Request, match: Mapping[str, str]) -> Response:
        body = _parse(RewindBody, request.body)
        self.manager.rewind(match["run_id"], body.checkpoint)
        return Response.json({"state": "requested", "checkpoint": body.checkpoint}, 202)

    # --- deliverables ------------------------------------------------------------------------

    def _outputs(self, run_id: str) -> tuple[RunRecord, Outputs]:
        record = self.manager.run(run_id)
        outputs = self.store.outputs(run_id)
        if outputs is None:
            if record.status is RunStatus.RUNNING:
                raise ConflictError("The run has not delivered anything yet.")
            raise ConflictError("This run delivered nothing.")
        return record, outputs

    def patch(self, request: Request, match: Mapping[str, str]) -> Response:
        _, outputs = self._outputs(match["run_id"])
        if outputs.blocked:
            raise LockedError(
                "The patch is withheld until you review it. Read the summary, then release it."
            )
        return Response.text(
            outputs.patch,
            "text/x-diff; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="sandcage.patch"'},
        )

    def summary(self, request: Request, match: Mapping[str, str]) -> Response:
        _, outputs = self._outputs(match["run_id"])
        return Response.text(outputs.summary_md, "text/markdown; charset=utf-8")

    def report(self, request: Request, match: Mapping[str, str]) -> Response:
        form = request.query.get("format", "json")
        if form not in {"json", "md"}:
            raise InvalidError("format must be json or md")
        record, outputs = self._outputs(match["run_id"])
        if form == "md":
            return Response.text(outputs.report_md, "text/markdown; charset=utf-8")
        report = dict(outputs.report)
        report["released_by_human"] = record.released_by_human or bool(
            report.get("released_by_human")
        )
        return Response.json(report)

    def release(self, request: Request, match: Mapping[str, str]) -> Response:
        self.manager.release(match["run_id"])
        return Response.json({"id": match["run_id"], "released_by_human": True})

    def learn(self, request: Request, match: Mapping[str, str]) -> Response:
        found = self.manager.learn(match["run_id"])
        if found is None:
            return Response.json({"suggestions": [], "notes": []})
        return Response.json(found.model_dump(mode="json", include={"suggestions", "notes"}))

    def accept(self, request: Request, match: Mapping[str, str]) -> Response:
        body = _parse(AcceptBody, request.body)
        rule = self.manager.accept(match["run_id"], body.suggestion)
        return Response.json({"rule_id": rule.id, "file": self.settings.learned_path.name})

    # --- the event stream ----------------------------------------------------------------------

    def events(self, request: Request, match: Mapping[str, str]) -> EventStream:
        record = self.manager.run(match["run_id"])
        raw = request.header("last-event-id") or request.query.get("after") or "0"
        if not raw.isdigit() or len(raw) > 18:
            raise InvalidError("Last-Event-ID must be an event id.")
        return EventStream(record.id, int(raw))

    def stream(self, stream: EventStream, write: Callable[[bytes], None]) -> None:
        """Replay everything after the resume point, then wait on the store (no polling),
        with a heartbeat comment when idle; end after ``done``. ``write`` raising
        ``OSError`` (the client left) ends it too."""
        last = stream.after_id
        write(b"retry: 2000\n\n")
        while True:
            done_id = self.store.last_event_id(stream.run_id, kind="done")
            if done_id and last >= done_id:
                return  # resumed at or after the end: nothing more will come
            batch = self.store.wait_for_events(stream.run_id, after_id=last, timeout=self.heartbeat)
            if not batch:
                write(b": ping\n\n")
                continue
            for event in batch:
                payload = event.raw if isinstance(event, UnknownEvent) else dump_event(event)
                data = json.dumps(payload, ensure_ascii=False)
                write(f"id: {event.id}\nevent: {event.kind}\ndata: {data}\n\n".encode())
                last = event.id
                if isinstance(event, DoneEvent):
                    return


# --- helpers -------------------------------------------------------------------------------


def _int(raw: str | None, default: int, name: str, low: int, high: int) -> int:
    if raw is None or raw == "":
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise InvalidError(f"{name} must be a whole number from {low} to {high}")
    return int(raw)


def _counts(steps: list[StepEvent]) -> DecisionCounts:
    return DecisionCounts(
        allowed=sum(1 for e in steps if e.decision is Decision.ALLOW),
        denied=sum(1 for e in steps if e.decision is Decision.DENY),
        asked=sum(1 for e in steps if e.decision is Decision.ASK),
        injection_tagged=sum(1 for e in steps if e.tags),
    )


def disk_check(path: Path) -> dict[str, JsonValue]:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return {"ok": False, "detail": "cannot read free disk space", "free_bytes": None}
    return {
        "ok": free >= MIN_FREE_BYTES,
        "detail": "at least 1 GiB free" if free >= MIN_FREE_BYTES else "less than 1 GiB free",
        "free_bytes": free,
    }
