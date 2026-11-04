"""The demo guard: every limit of the public demo behind one object.

- **Access code** on every changing request, compared in constant time. No code
  configured means open (local use). A missing code is 401, a wrong one 403; after
  :data:`WRONG_CODE_LIMIT` wrong codes in :data:`WRONG_CODE_WINDOW` seconds the client gets
  429 with ``Retry-After``, even with the right code.
- **Kill switch** (``paused``): starting a run is refused with 503 and a friendly message,
  checked before the code. Reading stays open.
- **Admission** (called under the run manager's lock, so two starts cannot both squeeze
  under a limit): runs per client per hour (sliding window, memory only), runs per UTC day
  and the daily token budget (both persisted in the event store). Runs in progress hold
  the unspent part of their token cap in reserve; a run needs :data:`MIN_HEADROOM` tokens
  of headroom and its cap shrinks to what is left.
- **Client identity** is the TCP peer, or behind our own proxy the *last*
  ``X-Forwarded-For`` hop (the first is whatever the visitor typed). IPs are never stored.

The access code never appears in a message, a log line or ``repr``.
"""

from __future__ import annotations

import hmac
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sandcage.api.settings import DemoSettings
from sandcage.events.store import EventStore, utc_day

WRONG_CODE_LIMIT: Final = 10
WRONG_CODE_WINDOW: Final = 600.0
HOUR: Final = 3600.0
MIN_HEADROOM: Final = 2000
MAX_TRACKED_CLIENTS: Final = 10_000


class RefusalError(Exception):
    """A request the demo guard refuses: an HTTP status, a message fit to show, and
    optionally how many seconds until trying again makes sense."""

    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class _Windows:
    """Timestamps per client in a sliding window; the number of tracked clients is
    bounded (the oldest is forgotten first), so a flood of addresses cannot grow memory."""

    def __init__(self, span: float) -> None:
        self.span = span
        self._seen: OrderedDict[str, deque[float]] = OrderedDict()

    def recent(self, client: str, now: float) -> deque[float]:
        found = self._seen.get(client)
        if found is None:
            return deque()
        while found and found[0] <= now - self.span:
            found.popleft()
        if not found:
            del self._seen[client]
        return found

    def add(self, client: str, now: float) -> None:
        found = self.recent(client, now)
        if client not in self._seen:
            self._seen[client] = found
        self._seen.move_to_end(client)
        found.append(now)
        while len(self._seen) > MAX_TRACKED_CLIENTS:
            self._seen.popitem(last=False)

    def retry_after(self, client: str, now: float) -> int:
        found = self.recent(client, now)
        return max(1, math.ceil(found[0] + self.span - now)) if found else 1


@dataclass
class _Reservation:
    cap: int
    spent: int = 0

    @property
    def remaining(self) -> int:
        return max(self.cap - self.spent, 0)


def client_id(peer: str, forwarded_for: str | None, *, trust_proxy: bool) -> str:
    """Who is asking: the TCP peer, or behind our own proxy the last forwarded-for hop."""
    if trust_proxy and forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last[:64]
    return peer[:64]


class DemoGuard:
    def __init__(
        self,
        settings: DemoSettings,
        store: EventStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._wrong = _Windows(WRONG_CODE_WINDOW)
        self._starts = _Windows(HOUR)
        self._reserved: dict[str, _Reservation] = {}

    def __repr__(self) -> str:
        return f"DemoGuard(public={self.settings.public}, paused={self.settings.paused})"

    @property
    def access_required(self) -> bool:
        return self.settings.access_code is not None

    def client(self, peer: str, forwarded_for: str | None) -> str:
        return client_id(peer, forwarded_for, trust_proxy=self.settings.trust_proxy)

    # --- the code ----------------------------------------------------------------------

    def check_code(self, client: str, supplied: str | None) -> None:
        """Raise :class:`RefusalError` unless the code is right (or none is configured)."""
        expected = self.settings.access_code
        if expected is None:
            return
        now = self._clock()
        with self._lock:
            if len(self._wrong.recent(client, now)) >= WRONG_CODE_LIMIT:
                raise RefusalError(
                    429,
                    "Too many wrong access codes. Try again later.",
                    self._wrong.retry_after(client, now),
                )
            if not supplied:
                raise RefusalError(401, "This demo needs an access code.")
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                self._wrong.add(client, now)
                raise RefusalError(403, "That access code is not right.")

    # --- the kill switch and admission ----------------------------------------------------

    def check_open(self) -> None:
        if self.settings.paused:
            raise RefusalError(503, self.settings.message)

    def admit(self, client: str, run_id: str) -> int:
        """Check every start limit and take this run's share; returns its token cap. The
        caller holds the run manager's lock and creates the run row right after."""
        now = self._clock()
        until_midnight = self._seconds_to_midnight(now)
        with self._lock:
            usage = self._store.daily_usage(utc_day(now))
            if usage.runs >= self.settings.runs_per_day:
                raise RefusalError(429, "Today's demo runs are used up.", until_midnight)
            if len(self._starts.recent(client, now)) >= self.settings.runs_per_client_per_hour:
                raise RefusalError(
                    429,
                    "You have started the most runs allowed in an hour.",
                    self._starts.retry_after(client, now),
                )
            reserved = sum(r.remaining for r in self._reserved.values())
            headroom = self.settings.day_token_cap - usage.tokens - reserved
            if headroom < MIN_HEADROOM:
                raise RefusalError(429, "Today's demo token budget is used up.", until_midnight)
            cap = min(self.settings.run_token_cap, headroom)
            self._starts.add(client, now)
            self._reserved[run_id] = _Reservation(cap)
        return cap

    def spend(self, run_id: str, tokens: int) -> None:
        """Tokens a run spent (also counted in the store's daily total by the caller)."""
        with self._lock:
            found = self._reserved.get(run_id)
            if found is not None:
                found.spent += tokens

    def finish(self, run_id: str) -> None:
        """The run ended: its unspent reservation goes back to the day."""
        with self._lock:
            self._reserved.pop(run_id, None)

    # --- what the panel shows -------------------------------------------------------------

    def status(self, *, active_runs: int) -> dict[str, object]:
        now = self._clock()
        settings = self.settings
        with self._lock:
            usage = self._store.daily_usage(utc_day(now))
        return {
            "access_required": self.access_required,
            "public": settings.public,
            "paused": settings.paused,
            "message": settings.message if settings.paused else None,
            "active_runs": active_runs,
            "max_concurrent": settings.max_concurrent,
            "runs_today": usage.runs,
            "runs_per_day": settings.runs_per_day,
            "runs_left_today": max(settings.runs_per_day - usage.runs, 0),
            "runs_per_client_per_hour": settings.runs_per_client_per_hour,
            "tokens_today": usage.tokens,
            "daily_token_cap": settings.day_token_cap,
            "run_token_cap": settings.run_token_cap,
            "resets_at": now + self._seconds_to_midnight(now),
        }

    @staticmethod
    def _seconds_to_midnight(now: float) -> int:
        today = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        midnight = (today + timedelta(days=1)).timestamp()
        return max(1, math.ceil(midnight - now))
