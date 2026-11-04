"""API settings, read once from the environment at startup. A malformed value stops the
API before it serves anything, naming the setting but never echoing its value.

Public mode fails closed: it refuses to start without an access code of at least
:data:`MIN_CODE_LENGTH` characters.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sandcage.agent.llm import DEFAULT_RETRIES

MIN_CODE_LENGTH: Final = 12
BODY_LIMIT: Final = 64 * 1024
APPROVAL_TIMEOUT: Final = 120.0
HEARTBEAT_SECONDS: Final = 15.0
STOP_WAIT: Final = 3.0

SCRIPTED_PROVIDERS: Final = frozenset({"scripted", "fake"})
OPENAI_PROVIDERS: Final = frozenset({"vultr", "openai"})
VULTR_BASE_URL: Final = "https://api.vultrinference.com/v1"

_ORIGIN: Final = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?")
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"", "0", "false", "no", "off"})


class SettingsError(ValueError):
    """A setting is missing, malformed or unsafe. The message names the setting only."""


@dataclass(frozen=True)
class DemoSettings:
    """Everything the demo guard enforces. Defaults are the hackathon's (DEPLOY.md)."""

    public: bool = False
    access_code: str | None = field(default=None, repr=False)
    paused: bool = False
    message: str = "The live demo is paused right now. Past runs stay readable."
    max_concurrent: int = 2
    runs_per_client_per_hour: int = 3
    runs_per_day: int = 40
    run_token_cap: int = 60_000
    day_token_cap: int = 2_000_000
    trust_proxy: bool = False

    def __post_init__(self) -> None:
        if self.public and (self.access_code is None or len(self.access_code) < MIN_CODE_LENGTH):
            raise SettingsError(
                f"public mode needs DEMO_ACCESS_CODE of at least {MIN_CODE_LENGTH} characters"
            )
        for name in (
            "max_concurrent",
            "runs_per_client_per_hour",
            "runs_per_day",
            "run_token_cap",
            "day_token_cap",
        ):
            if getattr(self, name) < 1:
                raise SettingsError(f"{name} must be at least 1")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DemoSettings:
        env = os.environ if environ is None else environ
        mode = env.get("DEMO_MODE", "local").strip().lower() or "local"
        if mode not in {"local", "public"}:
            raise SettingsError("DEMO_MODE must be 'local' or 'public'")
        code = env.get("DEMO_ACCESS_CODE", "").strip() or None
        if code is not None and not code.isprintable():
            raise SettingsError("DEMO_ACCESS_CODE must be printable text")
        return cls(
            public=mode == "public",
            access_code=code,
            paused=_flag(env, "DEMO_PAUSED"),
            max_concurrent=_count(env, "DEMO_MAX_CONCURRENT_RUNS", 2),
            runs_per_client_per_hour=_count(env, "DEMO_RUNS_PER_IP_PER_HOUR", 3),
            runs_per_day=_count(env, "DEMO_RUNS_PER_DAY", 40),
            run_token_cap=_count(env, "DEMO_RUN_TOKEN_CAP", 60_000),
            day_token_cap=_count(env, "DEMO_DAY_TOKEN_CAP", 2_000_000),
            trust_proxy=_flag(env, "DEMO_TRUST_PROXY"),
        )


@dataclass(frozen=True)
class ProviderSettings:
    """Which models runs use. ``scripted`` needs nothing and is the only provider that
    accepts a ``fake_scenario``; a real provider needs a key, which is never shown."""

    provider: str = "scripted"
    base_url: str = VULTR_BASE_URL
    api_key: str | None = field(default=None, repr=False)
    model: str | None = None
    #: How often a model call is retried after a 408, 409, 429 or 5xx (0 = never).
    retries: int = DEFAULT_RETRIES

    @property
    def scripted(self) -> bool:
        return self.provider in SCRIPTED_PROVIDERS

    @property
    def configured(self) -> bool:
        return self.scripted or bool(self.api_key and self.model)

    @property
    def problem(self) -> str | None:
        """Why runs cannot start (None when they can). Never contains the key."""
        if self.configured:
            return None
        return "The model provider is not configured (LLM_API_KEY and LLM_MODEL are needed)."

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        env = os.environ if environ is None else environ
        provider = env.get("LLM_PROVIDER", "scripted").strip().lower() or "scripted"
        if provider not in SCRIPTED_PROVIDERS | OPENAI_PROVIDERS:
            raise SettingsError("LLM_PROVIDER must be scripted, vultr or openai")
        base_url = env.get("LLM_BASE_URL", "").strip() or VULTR_BASE_URL
        if not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise SettingsError("LLM_BASE_URL must use https")
        return cls(
            provider=provider,
            base_url=base_url,
            api_key=env.get("LLM_API_KEY", "").strip() or None,
            model=env.get("LLM_MODEL", "").strip() or None,
            retries=_retries(env),
        )


@dataclass(frozen=True)
class ApiSettings:
    demo: DemoSettings = field(default_factory=DemoSettings)
    provider: ProviderSettings = field(default_factory=ProviderSettings)
    panel_origin: str | None = None
    db_path: Path = Path("sandcage.db")
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        if self.panel_origin is not None and not _ORIGIN.fullmatch(self.panel_origin):
            raise SettingsError("PANEL_ORIGIN must be an exact origin like https://host:port")

    @property
    def learned_path(self) -> Path:
        return self.data_dir / "learned.yaml"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ApiSettings:
        env = os.environ if environ is None else environ
        data_dir = Path(env.get("SANDCAGE_DATA_DIR", "").strip() or "data")
        db = env.get("SANDCAGE_DB", "").strip()
        port = _count(env, "SANDCAGE_API_PORT", 8000)
        if port > 65535:
            raise SettingsError("SANDCAGE_API_PORT must be a port number")
        return cls(
            demo=DemoSettings.from_env(env),
            provider=ProviderSettings.from_env(env),
            panel_origin=env.get("PANEL_ORIGIN", "").strip().rstrip("/") or None,
            db_path=Path(db) if db else data_dir / "sandcage.db",
            data_dir=data_dir,
            host=env.get("SANDCAGE_API_HOST", "").strip() or "127.0.0.1",
            port=port,
        )


def _flag(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise SettingsError(f"{name} must be true or false")


def _retries(env: Mapping[str, str]) -> int:
    raw = env.get("LLM_RETRIES", "").strip()
    if not raw:
        return DEFAULT_RETRIES
    if not raw.isdigit() or int(raw) > DEFAULT_RETRIES:
        raise SettingsError(f"LLM_RETRIES must be a whole number from 0 to {DEFAULT_RETRIES}")
    return int(raw)


def _count(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) < 1:
        raise SettingsError(f"{name} must be a whole number of at least 1")
    return int(raw)
