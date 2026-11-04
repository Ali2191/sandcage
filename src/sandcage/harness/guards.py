"""Run guards: budgets, the token meter, loop detection and the under-attack breaker.

They reach the agent loop only through its hooks (:meth:`Guards.hooks`):

- ``gate`` (before every model call, and again before every action): the stop flag, the
  time budget (human waiting excluded), the token budget, and loop detection;
- ``after_step``: progress for loop detection, then the breaker, which may pause the run
  for a human (one :func:`breaker_approval`) and ends it as ``killed`` on anything but yes;
- ``annotate``: the budget snapshot and breaker counts on every step event.

The step budget is the agent's own ``max_steps`` (it owns the counter); the guards report
it. Every model role is metered into one :class:`TokenMeter` through
:class:`MeteredClient`, which also refuses to call a model once the run is stopped.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

from sandcage.agent.llm import ChatClient, Completion, Message, ModelError, Usage
from sandcage.agent.loop import Approver, Hooks
from sandcage.agent.protocol import AgentAction
from sandcage.contract.approval import Approval, Resolution, breaker_approval
from sandcage.contract.common import Phase
from sandcage.contract.events import (
    BreakerCounts,
    BreakerEvent,
    BreakerState,
    Budget,
    Event,
    GuardEvent,
    RunStatus,
    StepEvent,
)

MAX_STEPS: Final = 25
MAX_SECONDS: Final = 600.0
MAX_TOKENS: Final = 150_000
CHARS_PER_TOKEN: Final = 4
LOOP_REPEATS: Final = 3
BREAKER_DENIALS: Final = 3
BREAKER_INJECTION_DENIALS: Final = 2
BREAKER_SHOWN: Final = 5
INJECTION_TAG: Final = "prompt_injection_suspected"
SECONDS_ENV: Final = "SANDCAGE_MAX_SECONDS"
TOKENS_ENV: Final = "LLM_MAX_TOKENS"

Emit = Callable[[Event], object]


class GuardConfigError(ValueError):
    """A budget override is not a positive number."""


# --- limits --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    max_steps: int = MAX_STEPS
    max_seconds: float = MAX_SECONDS
    max_tokens: int = MAX_TOKENS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Limits:
        env = os.environ if environ is None else environ
        return cls(
            max_seconds=_positive(env, SECONDS_ENV, float, MAX_SECONDS),
            max_tokens=int(_positive(env, TOKENS_ENV, int, MAX_TOKENS)),
        )

    def capped(self, max_tokens: int) -> Limits:
        """A tighter token limit (the demo's per-run cap); it can only lower the limit."""
        return Limits(self.max_steps, self.max_seconds, min(self.max_tokens, max_tokens))


def _positive(
    env: Mapping[str, str], name: str, kind: type[int] | type[float], default: float
) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = kind(raw.strip())
    except ValueError:
        raise GuardConfigError(f"{name} must be a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise GuardConfigError(f"{name} must be a positive number")
    return value


# --- time ----------------------------------------------------------------------------------


@dataclass
class RunClock:
    """Run time: wall time since the start, minus the time spent waiting for a human."""

    now: Callable[[], float] = time.monotonic
    _start: float | None = None
    _waited: float = 0.0
    _wait_started: float | None = None
    _depth: int = 0

    def start(self) -> None:
        if self._start is None:
            self._start = self.now()

    def elapsed(self) -> float:
        if self._start is None:
            return 0.0
        current = self.now()
        waiting = current - self._wait_started if self._wait_started is not None else 0.0
        return max(0.0, current - self._start - self._waited - waiting)

    @contextmanager
    def waiting(self) -> Iterator[None]:
        """Time inside this block does not count (nested blocks count once)."""
        if self._depth == 0:
            self._wait_started = self.now()
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and self._wait_started is not None:
                self._waited += self.now() - self._wait_started
                self._wait_started = None


# --- tokens --------------------------------------------------------------------------------


def estimate_tokens(chars: int) -> int:
    return math.ceil(max(chars, 0) / CHARS_PER_TOKEN)


@dataclass
class TokenMeter:
    """One meter for every role. ``reported`` stays true only while every counted token
    came from the provider; one estimate makes the total an estimate."""

    total: int = 0
    prompt: int = 0
    completion: int = 0
    reported: bool = True
    by_role: Counter[str] = field(default_factory=Counter)
    listeners: list[Callable[[int], None]] = field(default_factory=list)

    def add(self, role: str, usage: Usage | None, prompt_chars: int, completion_chars: int) -> int:
        if usage is not None:
            prompt, completion = usage.prompt_tokens, usage.completion_tokens
        else:
            prompt, completion = estimate_tokens(prompt_chars), estimate_tokens(completion_chars)
            self.reported = False
        added = prompt + completion
        self.prompt += prompt
        self.completion += completion
        self.total += added
        self.by_role[role] += added
        for listener in self.listeners:
            listener(added)
        return added


class RunStoppedError(ModelError):
    """The run is stopped, so no model may be called."""


@dataclass
class MeteredClient:
    """Wraps any role's chat client: refuses every call once the run is stopped, and
    counts each call into the shared meter."""

    inner: ChatClient
    role: str
    meter: TokenMeter
    stopped: Callable[[], bool]

    def complete(self, messages: Sequence[Message]) -> Completion:
        if self.stopped():
            raise RunStoppedError("the run was stopped, so the model was not called")
        completion = self.inner.complete(messages)
        prompt_chars = sum(len(message["content"]) for message in messages)
        self.meter.add(self.role, completion.usage, prompt_chars, len(completion.text))
        return completion


# --- loops ---------------------------------------------------------------------------------


def action_key(action: AgentAction) -> str:
    return json.dumps(action.to_raw(), sort_keys=True, separators=(",", ":"))


@dataclass
class LoopDetector:
    """The same tool and arguments proposed ``LOOP_REPEATS`` times since the last progress
    is a loop, caught before the last one runs. Repeats need not be consecutive. Progress
    forgets every count except writes, so fix-then-test cycles are fine but writing the
    same content three times is a loop."""

    repeats: int = LOOP_REPEATS
    counts: Counter[str] = field(default_factory=Counter)
    _writes: set[str] = field(default_factory=set)

    def propose(self, action: AgentAction) -> bool:
        """Count a proposal; True when it is a loop (it must not run)."""
        key = action_key(action)
        self.counts[key] += 1
        if action.tool == "write_file":
            self._writes.add(key)
        return self.counts[key] >= self.repeats

    def progress(self) -> None:
        self.counts = Counter({key: n for key, n in self.counts.items() if key in self._writes})


# --- the breaker ---------------------------------------------------------------------------


@dataclass
class Breaker:
    """Counts blocked steps since the last reset; trips at ``BREAKER_DENIALS`` blocks or
    ``BREAKER_INJECTION_DENIALS`` injection-tagged blocks. A block is a gate denial or an
    ask that was not approved (observed steps never count; a refused plan amendment is a
    human's answer about the plan and does not count)."""

    denials: int = 0
    injection_denials: int = 0
    trips: int = 0
    recent: list[StepEvent] = field(default_factory=list)

    def record(self, event: StepEvent) -> bool:
        """Count one step; True when the breaker trips."""
        if not event.blocked or event.observed or event.tool == "amend_plan":
            return False
        self.denials += 1
        if INJECTION_TAG in event.tags:
            self.injection_denials += 1
        self.recent = [*self.recent, event][-BREAKER_SHOWN:]
        tripped = (
            self.denials >= BREAKER_DENIALS or self.injection_denials >= BREAKER_INJECTION_DENIALS
        )
        if tripped:
            self.trips += 1
        return tripped

    def reset(self) -> None:
        self.denials = 0
        self.injection_denials = 0
        self.recent = []

    def counts(self) -> BreakerCounts:
        return BreakerCounts(
            denials=self.denials, injection_denials=self.injection_denials, trips=self.trips
        )

    def source(self) -> str | None:
        """The file or page steering the agent: the newest injection-tagged block's source."""
        for event in reversed(self.recent):
            if INJECTION_TAG in event.tags and event.source:
                return event.source
        return None

    def reason(self) -> str:
        if self.injection_denials >= BREAKER_INJECTION_DENIALS:
            return (
                f"{self.injection_denials} blocked actions were tagged as possible prompt injection"
            )
        return f"{self.denials} actions were blocked"

    def tried(self) -> list[str]:
        """Sandcage's own blocked sentences without "Tried to", oldest first."""
        phrases = []
        for event in self.recent:
            sentence = event.blocked_sentence or f"Tried to use {event.tool}"
            phrases.append(sentence.removeprefix("Tried to "))
        return phrases


# --- the hooks -----------------------------------------------------------------------------


@dataclass
class Guards:
    run_id: str
    limits: Limits = field(default_factory=Limits)
    approver: Approver | None = None
    emit: Emit | None = None
    clock: RunClock = field(default_factory=RunClock)
    wall: Callable[[], float] = time.time
    meter: TokenMeter = field(default_factory=TokenMeter)
    loops: LoopDetector = field(default_factory=LoopDetector)
    breaker: Breaker = field(default_factory=Breaker)
    steps: int = 0
    stop_reason: RunStatus | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        self.clock.start()

    # --- stop ----------------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def metered(self, client: ChatClient, role: str) -> MeteredClient:
        return MeteredClient(client, role, self.meter, self._stop.is_set)

    def wait_for_human(self, approver: Approver) -> Approver:
        """An approver whose waiting time does not count against the time budget."""

        def ask(approval: Approval) -> Resolution:
            with self.clock.waiting():
                return approver(approval)

        return ask

    def progress(self) -> None:
        """Verifier feedback or a human rewind: loop counts are forgotten (except writes)."""
        self.loops.progress()

    # --- hooks ---------------------------------------------------------------------------

    def hooks(self) -> Hooks:
        return Hooks(gate=self.gate, after_step=self.after_step, annotate=self.annotate)

    def gate(self, action: AgentAction | None) -> RunStatus | None:
        if self._stop.is_set():
            return RunStatus.STOPPED
        elapsed = self.clock.elapsed()
        if elapsed >= self.limits.max_seconds:
            return self._guard_stop(
                RunStatus.BUDGET_TIME,
                f"used {elapsed:.0f} of {self.limits.max_seconds:.0f} seconds",
            )
        if self.meter.total >= self.limits.max_tokens:
            return self._guard_stop(
                RunStatus.BUDGET_TOKENS,
                f"used {self.meter.total} of {self.limits.max_tokens} tokens",
            )
        if action is not None and self.loops.propose(action):
            return self._guard_stop(
                RunStatus.LOOP_REPEAT,
                f"the same {action.tool} action was proposed {self.loops.repeats} times "
                "without progress",
            )
        return None

    def after_step(self, event: StepEvent) -> RunStatus | None:
        self.steps = max(self.steps, event.step)
        if event.executed and event.tool in {"write_file", "rollback"}:
            self.loops.progress()
        if not self.breaker.record(event):
            return None
        return self._trip(event.step)

    def annotate(self, event: StepEvent) -> StepEvent:
        self.steps = max(self.steps, event.step)
        counts = self.breaker.counts()
        if event.blocked and not event.observed and event.tool != "amend_plan":
            # the breaker counts this step in after_step; show the count it will have
            injection = INJECTION_TAG in event.tags
            counts = BreakerCounts(
                denials=counts.denials + 1,
                injection_denials=counts.injection_denials + int(injection),
                trips=counts.trips,
            )
        return event.model_copy(update={"budget": self.budget(), "breaker": counts})

    def budget(self) -> Budget:
        return Budget(
            steps=self.steps,
            max_steps=self.limits.max_steps,
            tokens=self.meter.total,
            max_tokens=self.limits.max_tokens,
            tokens_reported=self.meter.reported,
            prompt_tokens=self.meter.prompt,
            completion_tokens=self.meter.completion,
            seconds=round(self.clock.elapsed(), 3),
            max_seconds=self.limits.max_seconds,
        )

    # --- internals -----------------------------------------------------------------------

    def _guard_stop(self, status: RunStatus, detail: str) -> RunStatus:
        if self.stop_reason is None:
            self.stop_reason = status
            self._emit(
                GuardEvent(
                    run_id=self.run_id,
                    phase=Phase.ACT,
                    ts=self.wall(),
                    budget=self.budget(),
                    stop_reason=status.value,
                    detail=detail,
                )
            )
        return status

    def _trip(self, step: int) -> RunStatus | None:
        breaker = self.breaker
        source = breaker.source()
        self._emit(self._breaker_event(BreakerState.TRIPPED, source))
        approval = breaker_approval(step, source=source, tried=breaker.tried())
        resumed = False
        if self.approver is not None and not self._stop.is_set():
            try:
                with self.clock.waiting():
                    resumed = self.approver(approval).approved
            except Exception:  # noqa: BLE001 - spec: an approver failure counts as no
                resumed = False
        if resumed and not self._stop.is_set():
            breaker.reset()
            self._emit(self._breaker_event(BreakerState.RESUMED, source))
            return None
        self._emit(self._breaker_event(BreakerState.KILLED, source))
        return RunStatus.STOPPED if self._stop.is_set() else RunStatus.KILLED

    def _breaker_event(self, state: BreakerState, source: str | None) -> BreakerEvent:
        breaker = self.breaker
        return BreakerEvent(
            run_id=self.run_id,
            phase=Phase.ACT,
            ts=self.wall(),
            budget=self.budget(),
            state=state,
            reason=breaker.reason(),
            source=source,
            denials=breaker.denials,
            injection_denials=breaker.injection_denials,
            trips=breaker.trips,
        )

    def _emit(self, event: Event) -> None:
        if self.emit is not None:
            self.emit(event)
