"""The demo guard and the API settings: fail-closed public mode, the access code (constant
time, rate-limited, never shown), the kill switch, and admission limits that survive a
restart."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandcage.api.guard import (
    MIN_HEADROOM,
    WRONG_CODE_LIMIT,
    DemoGuard,
    RefusalError,
    client_id,
)
from sandcage.api.settings import (
    MIN_CODE_LENGTH,
    ApiSettings,
    DemoSettings,
    ProviderSettings,
    SettingsError,
)
from sandcage.events.store import EventStore

CODE = "correct-horse-battery"
DAY = 1_793_000_000.0  # 2026-10-26 12:53 UTC


class Clock:
    def __init__(self, now: float = DAY) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _guard(tmp_path: Path, clock: Clock | None = None, **demo: object) -> DemoGuard:
    clock = clock or Clock()
    store = EventStore(tmp_path / "db.sqlite", clock=clock)
    settings = DemoSettings(access_code=CODE, **demo)  # type: ignore[arg-type]
    return DemoGuard(settings, store, clock=clock)


def _refusal(call: object, *args: object) -> RefusalError:
    assert callable(call)
    with pytest.raises(RefusalError) as caught:
        call(*args)
    return caught.value


# --- settings ----------------------------------------------------------------------------


def test_public_mode_refuses_to_start_without_a_long_enough_code() -> None:
    with pytest.raises(SettingsError, match="public mode"):
        DemoSettings.from_env({"DEMO_MODE": "public"})
    short = "x" * (MIN_CODE_LENGTH - 1)
    with pytest.raises(SettingsError, match="public mode") as caught:
        DemoSettings.from_env({"DEMO_MODE": "public", "DEMO_ACCESS_CODE": short})
    assert short not in str(caught.value)
    ok = DemoSettings.from_env({"DEMO_MODE": "public", "DEMO_ACCESS_CODE": "y" * MIN_CODE_LENGTH})
    assert ok.public
    assert ok.access_code == "y" * MIN_CODE_LENGTH


def test_local_mode_without_a_code_is_open() -> None:
    settings = DemoSettings.from_env({})
    assert (settings.public, settings.access_code) == (False, None)
    assert (settings.max_concurrent, settings.runs_per_day, settings.run_token_cap) == (
        2,
        40,
        60_000,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEMO_MODE", "open"),
        ("DEMO_PAUSED", "maybe"),
        ("DEMO_MAX_CONCURRENT_RUNS", "0"),
        ("DEMO_RUNS_PER_DAY", "-3"),
        ("DEMO_RUN_TOKEN_CAP", "lots"),
        ("DEMO_TRUST_PROXY", "2"),
        ("LLM_PROVIDER", "groq"),
        ("LLM_BASE_URL", "http://example.com/v1"),
        ("PANEL_ORIGIN", "https://panel.example/path"),
        ("SANDCAGE_API_PORT", "70000"),
    ],
)
def test_a_bad_setting_fails_loudly_naming_only_the_setting(name: str, value: str) -> None:
    with pytest.raises(SettingsError) as caught:
        ApiSettings.from_env({name: value})
    if len(value) > 2:
        assert value not in str(caught.value)


def test_the_code_is_never_in_a_repr() -> None:
    settings = DemoSettings(public=True, access_code=CODE)
    assert CODE not in repr(settings)
    assert CODE not in repr(ApiSettings(demo=settings))
    provider = ProviderSettings(provider="vultr", api_key="sk-secret-key", model="m")
    assert "sk-secret-key" not in repr(provider)
    assert provider.configured
    assert ProviderSettings(provider="vultr").problem is not None
    assert ProviderSettings().scripted


def test_panel_origin_is_exact() -> None:
    settings = ApiSettings.from_env({"PANEL_ORIGIN": "http://127.0.0.1:3000/"})
    assert settings.panel_origin == "http://127.0.0.1:3000"
    assert ApiSettings.from_env({}).panel_origin is None


# --- the code ------------------------------------------------------------------------------


def test_the_code_is_required_and_checked(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    assert _refusal(guard.check_code, "1.2.3.4", None).status == 401
    assert _refusal(guard.check_code, "1.2.3.4", "").status == 401
    wrong = _refusal(guard.check_code, "1.2.3.4", "nope")
    assert wrong.status == 403
    assert CODE not in wrong.message
    guard.check_code("1.2.3.4", CODE)


def test_no_code_configured_means_open(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "db.sqlite")
    guard = DemoGuard(DemoSettings(), store)
    guard.check_code("1.2.3.4", None)
    guard.check_code("1.2.3.4", "anything")
    assert guard.access_required is False


def test_too_many_wrong_codes_lock_the_client_out_then_expire(tmp_path: Path) -> None:
    clock = Clock()
    guard = _guard(tmp_path, clock)
    for _ in range(WRONG_CODE_LIMIT):
        assert _refusal(guard.check_code, "1.2.3.4", "wrong").status == 403
    locked = _refusal(guard.check_code, "1.2.3.4", CODE)  # even the right code now
    assert locked.status == 429
    assert locked.retry_after is not None
    assert 1 <= locked.retry_after <= 600
    guard.check_code("5.6.7.8", CODE)  # other clients are unaffected
    clock.now += 601
    guard.check_code("1.2.3.4", CODE)


def test_the_forwarded_for_header_counts_only_behind_our_proxy() -> None:
    forged = "6.6.6.6, 10.0.0.9"
    assert client_id("172.18.0.2", forged, trust_proxy=False) == "172.18.0.2"
    assert client_id("172.18.0.2", forged, trust_proxy=True) == "10.0.0.9"  # the last hop
    assert client_id("172.18.0.2", "", trust_proxy=True) == "172.18.0.2"
    assert client_id("172.18.0.2", None, trust_proxy=True) == "172.18.0.2"


# --- the kill switch and admission ---------------------------------------------------------


def test_paused_refuses_with_the_friendly_message(tmp_path: Path) -> None:
    guard = _guard(tmp_path, paused=True)
    refused = _refusal(guard.check_open)
    assert refused.status == 503
    assert "paused" in refused.message
    assert guard.status(active_runs=0)["paused"] is True


def test_runs_per_client_per_hour_is_a_sliding_window(tmp_path: Path) -> None:
    clock = Clock()
    guard = _guard(tmp_path, clock, runs_per_client_per_hour=2)
    guard.admit("1.2.3.4", "aaaaaaaaaaa1")
    clock.now += 1000
    guard.admit("1.2.3.4", "aaaaaaaaaaa2")
    refused = _refusal(guard.admit, "1.2.3.4", "aaaaaaaaaaa3")
    assert refused.status == 429
    assert refused.retry_after == 2600
    guard.admit("9.9.9.9", "bbbbbbbbbbb1")
    clock.now += 2601
    guard.admit("1.2.3.4", "aaaaaaaaaaa4")


def test_runs_per_day_come_from_the_store_and_survive_a_restart(tmp_path: Path) -> None:
    clock = Clock()
    store = EventStore(tmp_path / "db.sqlite", clock=clock)
    settings = DemoSettings(runs_per_day=2, runs_per_client_per_hour=10)
    guard = DemoGuard(settings, store, clock=clock)
    for run_id in ("aaaaaaaaaaa1", "aaaaaaaaaaa2"):
        guard.admit("1.2.3.4", run_id)
        store.create_run(run_id, repo="invoice-calc", task="t", guard=True)
    store.close()
    restarted = DemoGuard(settings, EventStore(tmp_path / "db.sqlite", clock=clock), clock=clock)
    refused = _refusal(restarted.admit, "5.5.5.5", "aaaaaaaaaaa3")
    assert refused.status == 429
    assert refused.retry_after is not None
    assert 0 < refused.retry_after <= 86_400
    clock.now += 86_400
    restarted.admit("5.5.5.5", "aaaaaaaaaaa3")


def test_the_run_cap_shrinks_to_the_days_headroom_and_runs_hold_a_reserve(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = EventStore(tmp_path / "db.sqlite", clock=clock)
    settings = DemoSettings(
        run_token_cap=60_000, day_token_cap=100_000, runs_per_client_per_hour=10
    )
    guard = DemoGuard(settings, store, clock=clock)
    assert guard.admit("1.1.1.1", "aaaaaaaaaaa1") == 60_000
    assert guard.admit("1.1.1.1", "aaaaaaaaaaa2") == 40_000  # the first holds 60k in reserve
    refused = _refusal(guard.admit, "1.1.1.1", "aaaaaaaaaaa3")
    assert (refused.status, "token" in refused.message) == (429, True)
    guard.spend("aaaaaaaaaaa1", 10_000)
    store.add_tokens(10_000)
    guard.finish("aaaaaaaaaaa1")  # 50k unspent goes back
    assert guard.admit("1.1.1.1", "aaaaaaaaaaa3") == 50_000


def test_a_run_needs_minimum_headroom(tmp_path: Path) -> None:
    clock = Clock()
    store = EventStore(tmp_path / "db.sqlite", clock=clock)
    guard = DemoGuard(DemoSettings(day_token_cap=10_000), store, clock=clock)
    store.add_tokens(10_000 - MIN_HEADROOM + 1)
    assert _refusal(guard.admit, "1.1.1.1", "aaaaaaaaaaa1").status == 429
    status = guard.status(active_runs=0)
    assert status["tokens_today"] == 10_000 - MIN_HEADROOM + 1


def test_status_reports_what_the_panel_needs(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    status = guard.status(active_runs=1)
    assert {
        "access_required",
        "paused",
        "message",
        "runs_left_today",
        "runs_per_day",
        "active_runs",
        "max_concurrent",
    } <= set(status)
    assert status["access_required"] is True
    assert status["active_runs"] == 1
    assert CODE not in str(status)
    assert CODE not in repr(guard)
