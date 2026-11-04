"""One agent run's execution surface: the workspace copy, the container and the gate.

Every proposed action goes through one :class:`PolicySession` (one per run):

- **enforce** (the default): only an allowed action executes. An ask is remembered, and
  :meth:`SandboxRun.execute_approved` runs exactly that action (nothing else) once a human
  approves it. Secret files are left out of the workspace copy.
- **observe** (the Bench baseline, and guard-off runs): the verdict is recorded but the
  action runs anyway, and the copy keeps its (fake) secret files, so attacks can land and
  the baseline is honest. The fetcher's address guard still applies in both modes.

File tools run on the host through the workspace (safefs); commands run in the
container; ``http_get`` runs in this process through the SSRF-safe fetcher and then the
quarantined reader, so the agent never sees the page. Every tool output is masked
(file contents with their path as the source) and so are the action args recorded for
events. Hooks: ``on_write`` (checkpoints), ``tools`` (extra tools such as rollback) and
``reader``. This module imports no agent or harness code.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Protocol

from sandcage.masking import JsonValue, mask, mask_value
from sandcage.policy import Decision, PolicyDecision, PolicyEngine, PolicySession, RuleLayer
from sandcage.policy.actions import Action, InvalidAction, Tool, parse_action
from sandcage.sandbox.container import CommandResult
from sandcage.sandbox.fetcher import Fetcher, FetchFailure, Hop, Page, UrlCheck
from sandcage.sandbox.workspace import Workspace, WorkspaceError, secret_checker

ANSWER_LIMIT: Final = 300


class GateMode(StrEnum):
    ENFORCE = "enforce"
    OBSERVE = "observe"


class CommandRunner(Protocol):
    """What runs commands: the Docker sandbox, or a fake in tests."""

    def start(self) -> None: ...

    def run(self, command: str) -> CommandResult: ...

    def close(self) -> None: ...


class PageFetcher(Protocol):
    def fetch(self, url: str) -> Page | FetchFailure: ...


@dataclass(frozen=True)
class ReaderAnswer:
    """The quarantined reader's answer about a page (the agent sees only this)."""

    answer: str
    contains_instructions: bool


class PageReader(Protocol):
    def __call__(self, question: str, page: Page) -> ReaderAnswer: ...


RunnerFactory = Callable[[Path], CommandRunner]
FetcherFactory = Callable[[UrlCheck], PageFetcher]
WriteHook = Callable[[str], int | None]
ToolHandler = Callable[[Action], JsonValue]


class RunClosedError(Exception):
    """An action was sent to a run that is not open."""


class ApprovalMismatchError(Exception):
    """Only the exact action that was just asked about may run after approval."""


@dataclass(frozen=True)
class StepResult:
    decision: PolicyDecision
    executed: bool
    result: JsonValue | None
    args: JsonValue
    observed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.to_dict(),
            "executed": self.executed,
            "observed": self.observed,
            "args": self.args,
            "result": self.result,
        }


@dataclass
class _Pending:
    canonical: str
    action: Action


@dataclass
class _State:
    workspace: Workspace
    runner: CommandRunner
    session: PolicySession
    fetcher: PageFetcher
    pending: _Pending | None = None
    egress: list[Hop] = field(default_factory=list)


class SandboxRun:
    def __init__(
        self,
        repo: Path,
        *,
        engine: PolicyEngine,
        runner_factory: RunnerFactory,
        mode: GateMode = GateMode.ENFORCE,
        layers: tuple[RuleLayer, ...] = (),
        may_edit_tests: bool = False,
        on_write: WriteHook | None = None,
        tools: Mapping[str, ToolHandler] | None = None,
        reader: PageReader | None = None,
        fetcher_factory: FetcherFactory | None = None,
        temp_root: Path | None = None,
    ) -> None:
        self._repo = repo
        self._engine = engine
        self._runner_factory = runner_factory
        self._mode = mode
        self._layers = layers
        self._may_edit_tests = may_edit_tests
        self._on_write = on_write
        self._tools = dict(tools or {})
        self._reader = reader
        self._fetcher_factory: FetcherFactory = fetcher_factory or Fetcher
        self._temp_root = temp_root
        self._state: _State | None = None
        self._finished = False

    # --- lifetime --------------------------------------------------------------------

    def open(self) -> SandboxRun:
        if self._state is not None or self._finished:
            raise RunClosedError("a run can be opened only once")
        context = self._engine.rulebook.context
        leave_out = secret_checker(context) if self._mode is GateMode.ENFORCE else None
        workspace = Workspace.create(self._repo, leave_out=leave_out, temp_root=self._temp_root)
        try:
            runner = self._runner_factory(workspace.path)
            runner.start()
        except BaseException:
            workspace.cleanup()
            raise
        session = PolicySession(
            self._engine,
            workspace=workspace,
            layers=self._layers,
            may_edit_tests=self._may_edit_tests,
        )
        fetcher = self._fetcher_factory(self._url_check(session))
        self._state = _State(workspace, runner, session, fetcher)
        return self

    def close(self) -> None:
        """Destroy the container and the temp workspace; both are attempted whatever
        fails, and the first failure is raised afterwards."""
        state, self._state = self._state, None
        self._finished = True
        if state is None:
            return
        try:
            state.runner.close()
        finally:
            state.workspace.cleanup()

    def __enter__(self) -> SandboxRun:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # --- what the harness reads ------------------------------------------------------

    @property
    def mode(self) -> GateMode:
        return self._mode

    @property
    def session(self) -> PolicySession:
        return self._open().session

    @property
    def workspace(self) -> Workspace:
        return self._open().workspace

    @property
    def egress(self) -> tuple[Hop, ...]:
        """Every web request this run made: URL (masked), host, address, status, bytes."""
        return tuple(self._open().egress)

    def add_layer(self, layer: RuleLayer) -> None:
        """Lock a layer (the approved plan) into the run's policy. Before :meth:`open` it
        joins the layers the session starts with; after, it goes through
        :meth:`PolicySession.add_layer`, which refuses once an action was evaluated."""
        if self._state is None:
            if self._finished:
                raise RunClosedError("the run is closed")
            self._layers = (*self._layers, layer)
            return
        self._state.session.add_layer(layer)

    def read_text(self, path: str) -> str | None:
        """The workspace view (for the diff guard and anything else that must see it)."""
        return self._open().workspace.read_text(path)

    # --- actions ---------------------------------------------------------------------

    def execute(self, raw: object) -> StepResult:
        state = self._open()
        state.pending = None
        decision = state.session.evaluate(raw)
        action = parse_action(raw)
        if isinstance(action, InvalidAction):
            return StepResult(decision, executed=False, result=None, args=None)
        args = _masked_args(action)
        if self._mode is GateMode.OBSERVE:
            return StepResult(decision, True, self._dispatch(state, action), args, observed=True)
        if decision.allowed:
            return StepResult(decision, True, self._dispatch(state, action), args)
        if decision.decision is Decision.ASK:
            state.pending = _Pending(_canonical(raw), action)
        return StepResult(decision, executed=False, result=None, args=args)

    def execute_approved(self, raw: object) -> StepResult:
        """Run the action that was just asked about, after a human approved it."""
        state = self._open()
        pending, state.pending = state.pending, None
        if pending is None or _canonical(raw) != pending.canonical:
            raise ApprovalMismatchError(
                "only the exact action that was just asked about can run after approval"
            )
        decision = state.session.trail[-1]
        return StepResult(
            decision, True, self._dispatch(state, pending.action), _masked_args(pending.action)
        )

    # --- dispatch --------------------------------------------------------------------

    def _dispatch(self, state: _State, action: Action) -> JsonValue:
        try:
            return self._tool(state, action)
        except WorkspaceError as problem:
            return {"error": str(problem)}

    def _tool(self, state: _State, action: Action) -> JsonValue:
        workspace = state.workspace
        tool = action.tool
        if tool == Tool.READ_FILE:
            read = workspace.read_file(action.path)
            source = workspace.relative(action.path)
            return {"content": mask(read.content, source=source), "truncated": read.truncated}
        if tool == Tool.WRITE_FILE:
            written = workspace.write_file(action.path, action.content)
            result: dict[str, JsonValue] = {"written": written}
            if self._on_write is not None:
                checkpoint = self._on_write(workspace.relative(action.path))
                if checkpoint is not None:
                    result["checkpoint"] = checkpoint
            return result
        if tool == Tool.LIST_DIR:
            entries: list[JsonValue] = [
                {"name": mask(entry.name), "type": entry.kind.value}
                for entry in workspace.list_dir(action.path)
            ]
            return {"entries": entries}
        if tool == Tool.RUN_COMMAND:
            outcome = state.runner.run(action.command)
            return {
                "exit_code": outcome.exit_code,
                "output": mask(outcome.output),
                "truncated": outcome.truncated,
                "timed_out": outcome.timed_out,
            }
        if tool == Tool.HTTP_GET:
            return self._read_page(state, action)
        if tool == Tool.FINISH:
            summary = action.args.get("summary", "")
            return {"finished": True, "summary": mask(summary if isinstance(summary, str) else "")}
        handler = self._tools.get(tool)
        if handler is None:
            return {"error": f"{tool} is not available in this run"}
        return mask_value(handler(action))

    def _read_page(self, state: _State, action: Action) -> JsonValue:
        if self._reader is None:
            return {"error": "web pages cannot be read in this run, so nothing was fetched"}
        outcome = state.fetcher.fetch(action.url)
        state.egress.extend(outcome.hops)
        if isinstance(outcome, FetchFailure):
            return {"error": outcome.reason}
        question = action.args.get("question", "")
        answer = self._reader(question if isinstance(question, str) else "", outcome)
        if answer.contains_instructions:
            state.session.mark_suspicious(action.url)
        return {
            "url": outcome.url,
            "status": outcome.status,
            "answer": mask(answer.answer[:ANSWER_LIMIT]),
            "contains_instructions": answer.contains_instructions,
        }

    # --- helpers ---------------------------------------------------------------------

    def _open(self) -> _State:
        if self._state is None:
            raise RunClosedError("the run is not open")
        return self._state

    def _url_check(self, session: PolicySession) -> UrlCheck:
        if self._mode is GateMode.OBSERVE:
            return lambda _url: None  # observe runs anything; the address guard still holds

        def check(url: str) -> str | None:
            decision = session.preview({"tool": Tool.HTTP_GET.value, "args": {"url": url}})
            return None if decision.allowed else decision.reason

        return check


def _json_safe(value: object) -> JsonValue:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _masked_args(action: Action) -> JsonValue:
    return mask_value(_json_safe(dict(action.args)))


def _canonical(raw: object) -> str:
    return json.dumps(_json_safe(raw), sort_keys=True, separators=(",", ":"))
