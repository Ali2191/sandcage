"""Shared helpers for sandbox tests: the demo repo and the rulebook's secret predicate."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

from sandcage.sandbox.workspace import secret_checker
from tests.policy_helpers import engine

DEMO_REPO: Final = Path(__file__).resolve().parents[1] / "demo-repos" / "invoice-calc"


def _demo_secret() -> str:
    """The fake key in the demo repo's .env (read, so no secret-shaped literal is here)."""
    for line in (DEMO_REPO / ".env").read_text().splitlines():
        name, _, value = line.partition("=")
        if name == "STRIPE_SECRET_KEY":
            return value
    raise AssertionError("the demo .env has no STRIPE_SECRET_KEY line")


DEMO_SECRET_VALUE: Final = _demo_secret()


def is_secret() -> Callable[[str], bool]:
    return secret_checker(engine().rulebook.context)
