"""Shared helpers for sandbox tests: the demo repo and the rulebook's secret predicate."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

from sandcage.sandbox.workspace import secret_checker
from tests.policy_helpers import engine

DEMO_REPO: Final = Path(__file__).resolve().parents[1] / "demo-repos" / "invoice-calc"
DEMO_SECRET_VALUE: Final = "sk_live_FAKE0demo0only0notreal"


def is_secret() -> Callable[[str], bool]:
    return secret_checker(engine().rulebook.context)
