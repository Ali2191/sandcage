"""The SQLite event log and run records: the single source of truth the panel replays.

- **runs**: one row per run (status ``running`` until it finishes; the final ``done``
  payload is kept as the summary so the run list never recomputes from events).
- **events**: every event, in order. The autoincrement id is the stream id, so resume is
  "everything after id N" and ids never repeat. The full contract payload is in ``data``;
  the flat columns exist for querying.
- **outputs**: patch, SUMMARY.md, the blocked flag, report (JSON and Markdown), learn.
- **daily_usage**: runs started and tokens spent per UTC day. Tokens are added as they are
  spent, so a run cut off by a restart still counts.
- ``runs.released_by_human`` (v4): a release happens after ``done``, so it cannot be an
  event; the run row carries it.

Schema changes are numbered migrations tracked in ``PRAGMA user_version``; an older file
is upgraded on open, and a file from a newer version is refused (fail closed). One shared
connection guarded by a lock; :meth:`EventStore.wait_for_events` blocks on a condition
that every append notifies, so a stream waits instead of polling.

Assumes exactly one process per database file.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import JsonValue

from sandcage.contract.events import (
    DoneEvent,
    Event,
    RunStatus,
    UnknownEvent,
    dump_event,
    parse_event,
)
from sandcage.masking import mask

RUN_ID: Final = re.compile(r"[0-9a-f]{12}")
MAX_PAGE: Final = 100
_SELECT_RUNS: Final = (
    "SELECT id, repo, task, guard, status, created, finished, provider, model, tokens,"
    " summary, released_by_human FROM runs"
)


class StoreError(Exception):
    """The store cannot do what was asked (unknown run, bad id, newer schema)."""


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One explicit transaction (the connection runs in autocommit mode otherwise)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


# --- migrations ---------------------------------------------------------------------------


def _v1_runs_and_events(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            task TEXT NOT NULL,
            guard INTEGER NOT NULL,
            status TEXT NOT NULL,
            created REAL NOT NULL,
            finished REAL,
            summary TEXT,
            provider TEXT,
            model TEXT,
            tokens INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db.execute("CREATE INDEX runs_by_created ON runs (created)")
    db.execute(
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs (id),
            kind TEXT NOT NULL,
            step INTEGER,
            tool TEXT,
            decision TEXT,
            rule_id TEXT,
            ts REAL NOT NULL,
            data TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX events_by_run ON events (run_id, id)")


def _v2_outputs(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE outputs (
            run_id TEXT PRIMARY KEY REFERENCES runs (id),
            patch TEXT NOT NULL,
            summary_md TEXT NOT NULL,
            blocked INTEGER NOT NULL,
            report TEXT NOT NULL,
            report_md TEXT NOT NULL,
            learn TEXT NOT NULL
        )"""
    )


def _v3_daily_usage(db: sqlite3.Connection) -> None:
    """Daily counters, backfilled from the runs already in the file."""
    db.execute(
        "CREATE TABLE daily_usage"
        " (day TEXT PRIMARY KEY, runs INTEGER NOT NULL, tokens INTEGER NOT NULL)"
    )
    totals: dict[str, list[int]] = {}
    for created, tokens in db.execute("SELECT created, tokens FROM runs"):
        day = totals.setdefault(utc_day(created), [0, 0])
        day[0] += 1
        day[1] += tokens
    db.executemany(
        "INSERT INTO daily_usage (day, runs, tokens) VALUES (?, ?, ?)",
        [(day, runs, tokens) for day, (runs, tokens) in sorted(totals.items())],
    )


def _v4_released_by_human(db: sqlite3.Connection) -> None:
    """A release comes after ``done``, so the run row carries it (backfilled from the
    reports of runs released before this column existed)."""
    db.execute("ALTER TABLE runs ADD COLUMN released_by_human INTEGER NOT NULL DEFAULT 0")
    released = [
        run_id
        for run_id, report in db.execute("SELECT run_id, report FROM outputs")
        if isinstance(found := json.loads(report), dict) and found.get("released_by_human") is True
    ]
    db.executemany("UPDATE runs SET released_by_human = 1 WHERE id = ?", [(r,) for r in released])


MIGRATIONS: Final[tuple[Callable[[sqlite3.Connection], None], ...]] = (
    _v1_runs_and_events,
    _v2_outputs,
    _v3_daily_usage,
    _v4_released_by_human,
)
SCHEMA_VERSION: Final = len(MIGRATIONS)


def migrate(db: sqlite3.Connection, target: int = SCHEMA_VERSION) -> int:
    """Apply every migration after the file's version, each in its own transaction."""
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise StoreError(
            f"the database is at schema version {version}, newer than this build ({SCHEMA_VERSION})"
        )
    for number in range(version + 1, target + 1):
        with transaction(db):
            MIGRATIONS[number - 1](db)
            db.execute(f"PRAGMA user_version = {number}")
    return max(version, target)


# --- records -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    id: str
    repo: str
    task: str
    guard: bool
    status: RunStatus
    created: float
    finished: float | None
    provider: str | None
    model: str | None
    tokens: int
    done: DoneEvent | None
    released_by_human: bool = False


@dataclass(frozen=True)
class Outputs:
    patch: str
    summary_md: str
    blocked: bool
    report: dict[str, JsonValue]
    report_md: str
    learn: dict[str, JsonValue]


@dataclass(frozen=True)
class DailyUsage:
    day: str
    runs: int
    tokens: int


# --- the store -----------------------------------------------------------------------------


class EventStore:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._clock = clock
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        try:
            migrate(self._db)
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- runs --------------------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        *,
        repo: str,
        task: str,
        guard: bool,
        provider: str | None = None,
        model: str | None = None,
    ) -> RunRecord:
        """A new running run, counted as one of today's runs."""
        _check_id(run_id)
        now = self._clock()
        with self._lock, transaction(self._db):
            self._db.execute(
                "INSERT INTO runs (id, repo, task, guard, status, created, provider, model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    repo,
                    mask(task),
                    int(guard),
                    RunStatus.RUNNING.value,
                    now,
                    provider,
                    model,
                ),
            )
            self._bump(utc_day(now), runs=1, tokens=0)
        return self.get_run(run_id)

    def finish_run(self, run_id: str, done: DoneEvent, *, tokens: int) -> None:
        """Record the final status and summary. The ``done`` event itself is appended
        separately, after the run's slot is freed."""
        with self._lock, transaction(self._db):
            changed = self._db.execute(
                "UPDATE runs SET status = ?, finished = ?, summary = ?, tokens = ? WHERE id = ?",
                (done.status.value, self._clock(), json.dumps(dump_event(done)), tokens, run_id),
            ).rowcount
        if not changed:
            raise StoreError(f"no run {run_id}")

    def get_run(self, run_id: str) -> RunRecord:
        _check_id(run_id)
        with self._lock:
            row = self._db.execute(
                _SELECT_RUNS + " WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StoreError(f"no run {run_id}")
        return _run(row)

    def list_runs(self, *, limit: int = 20, offset: int = 0) -> tuple[list[RunRecord], int]:
        """A page of runs, newest first, and the total count."""
        if not 1 <= limit <= MAX_PAGE or offset < 0:
            raise StoreError(f"limit must be 1 to {MAX_PAGE} and offset at least 0")
        with self._lock:
            rows = self._db.execute(
                _SELECT_RUNS + " ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            (total,) = self._db.execute("SELECT COUNT(*) FROM runs").fetchone()
        return [_run(row) for row in rows], total

    def running_runs(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM runs WHERE status = ? ORDER BY created", (RunStatus.RUNNING.value,)
            ).fetchall()
        return [row[0] for row in rows]

    # --- events ------------------------------------------------------------------------

    def append(self, event: Event) -> Event:
        """Store one event; returns it with its stream id. Wakes every waiting stream."""
        if not RUN_ID.fullmatch(event.run_id):
            raise StoreError("an event needs the 12-hex-character id of its run")
        with self._lock:
            try:
                stored = self._insert(event)
            except sqlite3.IntegrityError:
                raise StoreError(f"no run {event.run_id}") from None
            self._changed.notify_all()
        return stored

    def _insert(self, event: Event) -> Event:
        with transaction(self._db):
            cursor = self._db.execute(
                "INSERT INTO events (run_id, kind, step, tool, decision, rule_id, ts, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '')",
                (
                    event.run_id,
                    event.kind,
                    getattr(event, "step", None),
                    getattr(event, "tool", None),
                    _value(getattr(event, "decision", None)),
                    getattr(event, "rule_id", None),
                    event.ts,
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                raise StoreError("the event was not stored")
            stored = event.model_copy(update={"id": event_id})
            self._db.execute(
                "UPDATE events SET data = ? WHERE id = ?",
                (json.dumps(dump_event(stored), ensure_ascii=False), event_id),
            )
        return stored

    def events(self, run_id: str, *, after_id: int = 0) -> list[Event | UnknownEvent]:
        """The run's events after ``after_id`` (the resume point), in order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM events WHERE run_id = ? AND id > ? ORDER BY id",
                (run_id, after_id),
            ).fetchall()
        return [parse_event(json.loads(row[0])) for row in rows]

    def last_event_id(self, run_id: str, *, kind: str | None = None) -> int:
        """The newest stored event id of the run (of one kind), or 0 when there is none."""
        query = "SELECT MAX(id) FROM events WHERE run_id = ?"
        params: tuple[str, ...] = (run_id,)
        if kind is not None:
            query += " AND kind = ?"
            params = (run_id, kind)
        with self._lock:
            (found,) = self._db.execute(query, params).fetchone()
        return int(found or 0)

    def wait_for_events(
        self, run_id: str, *, after_id: int, timeout: float
    ) -> list[Event | UnknownEvent]:
        """Events after ``after_id``, waiting up to ``timeout`` seconds for the first one.
        Returns an empty list on timeout (the stream then sends a heartbeat)."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                found = self.events(run_id, after_id=after_id)
                remaining = deadline - time.monotonic()
                if found or remaining <= 0:
                    return found
                self._changed.wait(remaining)

    # --- outputs -----------------------------------------------------------------------

    def save_outputs(self, run_id: str, outputs: Outputs) -> None:
        with self._lock, transaction(self._db):
            self._db.execute(
                "INSERT OR REPLACE INTO outputs"
                " (run_id, patch, summary_md, blocked, report, report_md, learn)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    outputs.patch,
                    outputs.summary_md,
                    int(outputs.blocked),
                    json.dumps(outputs.report),
                    outputs.report_md,
                    json.dumps(outputs.learn),
                ),
            )

    def outputs(self, run_id: str) -> Outputs | None:
        with self._lock:
            row = self._db.execute(
                "SELECT patch, summary_md, blocked, report, report_md, learn"
                " FROM outputs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        patch, summary_md, blocked, report, report_md, learn = row
        return Outputs(
            patch, summary_md, bool(blocked), json.loads(report), report_md, json.loads(learn)
        )

    def release(self, run_id: str, note: str) -> bool:
        """A human releases a withheld patch: unblock it and append ``note`` to the summary
        and the report. False when there is nothing blocked to release."""
        with self._lock, transaction(self._db):
            row = self._db.execute(
                "SELECT summary_md, report, report_md FROM outputs"
                " WHERE run_id = ? AND blocked = 1",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            summary_md, report_json, report_md = row
            report = json.loads(report_json)
            if isinstance(report, dict):
                report["released_by_human"] = True
                report["delivery_blocked"] = False
            line = f"\n\n{mask(note)}\n"
            self._db.execute(
                "UPDATE outputs SET blocked = 0, summary_md = ?, report = ?, report_md = ?"
                " WHERE run_id = ?",
                (summary_md.rstrip() + line, json.dumps(report), report_md.rstrip() + line, run_id),
            )
            self._db.execute("UPDATE runs SET released_by_human = 1 WHERE id = ?", (run_id,))
        return True

    # --- daily counters ----------------------------------------------------------------

    def add_tokens(self, tokens: int, *, ts: float | None = None) -> None:
        """Count tokens toward the UTC day they were spent on, as they are spent."""
        if tokens < 0:
            raise StoreError("tokens cannot be negative")
        with self._lock, transaction(self._db):
            self._bump(utc_day(self._clock() if ts is None else ts), runs=0, tokens=tokens)

    def daily_usage(self, day: str) -> DailyUsage:
        with self._lock:
            row = self._db.execute(
                "SELECT runs, tokens FROM daily_usage WHERE day = ?", (day,)
            ).fetchone()
        return DailyUsage(day, *(row or (0, 0)))

    def _bump(self, day: str, *, runs: int, tokens: int) -> None:
        self._db.execute(
            "INSERT INTO daily_usage (day, runs, tokens) VALUES (?, ?, ?)"
            " ON CONFLICT (day) DO UPDATE SET runs = runs + excluded.runs,"
            " tokens = tokens + excluded.tokens",
            (day, runs, tokens),
        )


def _run(row: tuple[Any, ...]) -> RunRecord:
    (
        run_id,
        repo,
        task,
        guard,
        status,
        created,
        finished,
        provider,
        model,
        tokens,
        summary,
        released,
    ) = row
    done = None
    if isinstance(summary, str):
        parsed = parse_event(json.loads(summary))
        done = parsed if isinstance(parsed, DoneEvent) else None
    return RunRecord(
        id=str(run_id),
        repo=str(repo),
        task=str(task),
        guard=bool(guard),
        status=RunStatus(str(status)),
        created=float(created),
        finished=None if finished is None else float(finished),
        provider=None if provider is None else str(provider),
        model=None if model is None else str(model),
        tokens=int(tokens),
        done=done,
        released_by_human=bool(released),
    )


def _check_id(run_id: str) -> None:
    if not RUN_ID.fullmatch(run_id):
        raise StoreError("a run id is 12 hex characters")


def _value(decision: object) -> str | None:
    return None if decision is None else str(getattr(decision, "value", decision))
