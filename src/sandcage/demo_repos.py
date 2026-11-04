"""Where the demo repos live (``demo-repos/`` at the repository root). Runs may target
these names only; a request never names a path."""

from __future__ import annotations

from pathlib import Path
from typing import Final

DEMO_REPOS_DIR: Final = Path(__file__).resolve().parents[2] / "demo-repos"
INVOICE_CALC: Final = DEMO_REPOS_DIR / "invoice-calc"
