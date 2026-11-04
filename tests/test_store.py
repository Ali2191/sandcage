"""The event store: ordering, resume, the wait notification, migrations, daily counters,
outputs and release."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sandcage.contract.events import DoneEvent, RunStatus, StepEvent, UnknownEvent
from sandcage.contract.sample import RUN_ID, demo_events
from sandcage.events.store import (
    SCHEMA_VERSION,
    EventStore,
    Outputs,
    StoreError,
    migrate,
    utc_day,
)
from tests.secrets_fixtures import GITHUB_TOKEN

DAY_ONE = 1_793_000_000.0  # 2026-10-26 UTC
OTHER = "0123456789ab"


class Clock:
    def __init__(self, now: float = DAY_ONE) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "sandcage.db", clock=Clock())


def _start(store: EventStore, run_id: str = RUN_ID) -> None:
    store.create_run(run_id, repo="invoice-calc", task="fix it", guard=True, provider="fake")


def _done(run_id: str = RUN_ID) -> DoneEvent:
    done = demo_events()[-1]
    assert isinstance(done, DoneEvent)
    return done.model_copy(update={"run_id": run_id, "id": 0})


def test_events_are_stored_in_order_with_increasing_ids(store: EventStore) -> None:
    _start(store)
    _start(store, OTHER)
    stored = []
    for event in demo_events():
        stored.append(store.append(event.model_copy(update={"id": 0})))
        store.append(event.model_copy(update={"id": 0, "run_id": OTHER}))
    ids = [event.id for event in stored]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    replay = store.events(RUN_ID)
    assert [event.id for event in replay] == ids
    assert [event.kind for event in replay] == [event.kind for event in demo_events()]
    assert all(event.run_id == RUN_ID for event in replay)
    resumed = store.events(RUN_ID, after_id=ids[9])
    assert [event.id for event in resumed] == ids[10:]


def test_flat_columns_are_queryable(store: EventStore, tmp_path: Path) -> None:
    _start(store)
    for event in demo_events():
        store.append(event)
    db = sqlite3.connect(tmp_path / "sandcage.db")
    rows = db.execute(
        "SELECT step, tool, decision, rule_id FROM events WHERE kind = 'step' AND decision = 'deny'"
    ).fetchall()
    assert rows == [
        (3, "run_command", "deny", "net.deny_tools"),
        (4, "read_file", "deny", "path.deny_secrets"),
    ]


def test_unknown_kinds_in_the_log_are_kept(store: EventStore, tmp_path: Path) -> None:
    _start(store)
    db = sqlite3.connect(tmp_path / "sandcage.db")
    with db:
        db.execute(
            "INSERT INTO events (run_id, kind, ts, data) VALUES (?, 'later', 1, ?)",
            (RUN_ID, f'{{"kind": "later", "run_id": "{RUN_ID}", "id": 1}}'),
        )
    (event,) = store.events(RUN_ID)
    assert isinstance(event, UnknownEvent)


def test_append_needs_a_known_run_and_a_valid_id(store: EventStore) -> None:
    event = demo_events()[0]
    with pytest.raises(StoreError, match="no run"):
        store.append(event)
    with pytest.raises(StoreError, match="12-hex"):
        store.append(event.model_copy(update={"run_id": "../../etc"}))
    with pytest.raises(StoreError, match="12 hex"):
        store.create_run("nope", repo="r", task="t", guard=True)


def test_wait_returns_at_once_times_out_or_wakes_on_append(store: EventStore) -> None:
    _start(store)
    first = store.append(demo_events()[0])
    assert [e.id for e in store.wait_for_events(RUN_ID, after_id=0, timeout=5)] == [first.id]
    started = time.monotonic()
    assert store.wait_for_events(RUN_ID, after_id=first.id, timeout=0.05) == []
    assert time.monotonic() - started >= 0.05
    writer = threading.Thread(target=lambda: store.append(demo_events()[1]))
    started = time.monotonic()
    writer.start()
    woken = store.wait_for_events(RUN_ID, after_id=first.id, timeout=10)
    writer.join()
    assert [e.kind for e in woken] == ["scout"]
    assert time.monotonic() - started < 5  # woken by the append, not the timeout


def test_runs_lifecycle_listing_and_running(store: EventStore) -> None:
    clock = Clock()
    store = EventStore(":memory:", clock=clock)
    for index in range(3):
        clock.now = DAY_ONE + index
        _start(store, f"{index:012x}")
    assert store.running_runs() == ["000000000000", "000000000001", "000000000002"]
    store.finish_run("000000000001", _done("000000000001"), tokens=1234)
    record = store.get_run("000000000001")
    assert record.status is RunStatus.FINISHED
    assert record.tokens == 1234
    assert record.done is not None
    assert record.done.steps == 9
    page, total = store.list_runs(limit=2)
    assert total == 3
    assert [r.id for r in page] == ["000000000002", "000000000001"]
    assert [r.id for r in store.list_runs(limit=2, offset=2)[0]] == ["000000000000"]
    assert store.running_runs() == ["000000000000", "000000000002"]
    with pytest.raises(StoreError):
        store.list_runs(limit=101)
    with pytest.raises(StoreError, match="no run"):
        store.get_run("ffffffffffff")
    with pytest.raises(StoreError, match="no run"):
        store.finish_run("ffffffffffff", _done("ffffffffffff"), tokens=0)


def test_the_task_is_masked_when_stored(store: EventStore) -> None:
    store.create_run(RUN_ID, repo="r", task=f"use {GITHUB_TOKEN}", guard=False)
    assert GITHUB_TOKEN not in store.get_run(RUN_ID).task


def test_daily_counters_count_runs_and_tokens_and_survive_a_restart(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "db.sqlite"
    store = EventStore(path, clock=clock)
    _start(store)
    store.add_tokens(500)
    store.add_tokens(250)
    clock.now = DAY_ONE + 86_400
    _start(store, OTHER)
    store.add_tokens(10)
    store.close()
    reopened = EventStore(path, clock=clock)
    day_one, day_two = utc_day(DAY_ONE), utc_day(DAY_ONE + 86_400)
    assert reopened.daily_usage(day_one).runs == 1
    assert reopened.daily_usage(day_one).tokens == 750
    assert (reopened.daily_usage(day_two).runs, reopened.daily_usage(day_two).tokens) == (1, 10)
    assert reopened.daily_usage("2000-01-01").runs == 0
    with pytest.raises(StoreError):
        reopened.add_tokens(-1)


# --- migrations ------------------------------------------------------------------------------


def test_a_new_file_gets_the_latest_schema_and_reopening_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    EventStore(path).close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "events", "outputs", "daily_usage"} <= tables
    EventStore(path).close()
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_an_old_file_is_migrated_and_its_counters_backfilled(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path, isolation_level=None)
    migrate(db, target=1)  # a file from before outputs and daily counters existed
    db.execute(
        "INSERT INTO runs (id, repo, task, guard, status, created, tokens) VALUES"
        " ('aaaaaaaaaaaa', 'r', 't', 1, 'finished', ?, 100),"
        " ('bbbbbbbbbbbb', 'r', 't', 1, 'finished', ?, 50),"
        " ('cccccccccccc', 'r', 't', 1, 'finished', ?, 7)",
        (DAY_ONE, DAY_ONE + 60, DAY_ONE + 86_400),
    )
    db.close()
    store = EventStore(path)
    usage = store.daily_usage(utc_day(DAY_ONE))
    assert (usage.runs, usage.tokens) == (2, 150)
    assert store.daily_usage(utc_day(DAY_ONE + 86_400)).tokens == 7
    assert store.outputs("aaaaaaaaaaaa") is None
    assert store.get_run("aaaaaaaaaaaa").status is RunStatus.FINISHED


def test_a_file_from_a_newer_build_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "new.sqlite"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(StoreError, match="newer than this build"):
        EventStore(path)


def test_a_failing_migration_leaves_nothing_half_done(tmp_path: Path) -> None:
    path = tmp_path / "half.sqlite"
    db = sqlite3.connect(path, isolation_level=None)
    migrate(db, target=2)
    db.execute("CREATE TABLE daily_usage (day TEXT)")  # makes migration 3 fail
    db.execute("INSERT INTO daily_usage VALUES ('x')")
    with pytest.raises(sqlite3.OperationalError):
        migrate(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert db.execute("SELECT * FROM daily_usage").fetchall() == [("x",)]


# --- outputs and release ---------------------------------------------------------------------


def _outputs(blocked: bool) -> Outputs:
    return Outputs(
        patch="--- a\n+++ b\n",
        summary_md="# Summary\nWithheld.",
        blocked=blocked,
        report={"delivery_blocked": blocked, "released_by_human": False},
        report_md="# Report",
        learn={"suggestions": []},
    )


def test_outputs_round_trip_and_replace(store: EventStore) -> None:
    _start(store)
    store.save_outputs(RUN_ID, _outputs(False))
    assert store.outputs(RUN_ID) == _outputs(False)
    store.save_outputs(RUN_ID, _outputs(True))
    saved = store.outputs(RUN_ID)
    assert saved is not None
    assert saved.blocked


def test_release_flips_blocked_and_notes_it_in_summary_and_report(store: EventStore) -> None:
    _start(store)
    store.save_outputs(RUN_ID, _outputs(True))
    assert store.release(RUN_ID, f"Released by a human. {GITHUB_TOKEN}") is True
    released = store.outputs(RUN_ID)
    assert released is not None
    assert released.blocked is False
    assert released.summary_md.endswith("Released by a human. [MASKED:github_token]\n")
    assert released.report_md.startswith("# Report")
    assert "Released by a human" in released.report_md
    assert released.report == {"delivery_blocked": False, "released_by_human": True}
    assert store.get_run(RUN_ID).released_by_human is True  # the run row carries it
    assert store.release(RUN_ID, "again") is False
    assert store.release(OTHER, "nothing there") is False


def test_events_from_the_store_are_typed(store: EventStore) -> None:
    _start(store)
    for event in demo_events():
        store.append(event)
    steps = [e for e in store.events(RUN_ID) if isinstance(e, StepEvent)]
    assert [s.step for s in steps] == list(range(1, 10))


def test_a_new_run_is_not_released(store: EventStore) -> None:
    _start(store)
    assert store.get_run(RUN_ID).released_by_human is False
    store.save_outputs(RUN_ID, _outputs(False))
    assert store.release(RUN_ID, "nothing was blocked") is False
    assert store.get_run(RUN_ID).released_by_human is False


def test_v4_backfills_released_by_human_from_reports(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite"
    db = sqlite3.connect(path, isolation_level=None)
    migrate(db, target=3)
    db.execute(
        "INSERT INTO runs (id, repo, task, guard, status, created, tokens) VALUES"
        " ('aaaaaaaaaaaa', 'r', 't', 1, 'finished', ?, 0),"
        " ('bbbbbbbbbbbb', 'r', 't', 1, 'finished', ?, 0)",
        (DAY_ONE, DAY_ONE),
    )
    for run_id, released in (("aaaaaaaaaaaa", True), ("bbbbbbbbbbbb", False)):
        db.execute(
            "INSERT INTO outputs VALUES (?, '', '', 0, ?, '', '{}')",
            (run_id, '{"released_by_human": %s}' % ("true" if released else "false")),
        )
    db.close()
    store = EventStore(path)
    assert store.get_run("aaaaaaaaaaaa").released_by_human is True
    assert store.get_run("bbbbbbbbbbbb").released_by_human is False


def test_last_event_id_of_a_run_and_of_one_kind(store: EventStore) -> None:
    _start(store)
    _start(store, OTHER)
    assert store.last_event_id(RUN_ID) == 0
    assert store.last_event_id(RUN_ID, kind="done") == 0
    stored = [store.append(e.model_copy(update={"id": 0})) for e in demo_events()]
    store.append(demo_events()[0].model_copy(update={"id": 0, "run_id": OTHER}))
    assert store.last_event_id(RUN_ID) == stored[-1].id
    assert store.last_event_id(RUN_ID, kind="done") == stored[-1].id
    assert store.last_event_id(RUN_ID, kind="understand") == stored[0].id
