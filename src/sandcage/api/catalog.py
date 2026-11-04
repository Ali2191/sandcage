"""Read-only catalogue data: demo repos, the rulebook, Bench scenarios and results.

The Bench (S8) does not exist yet, so the scenario list is empty and the results are
absent (404) until a Bench run writes its file; nothing is invented to fill the gallery.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from sandcage.policy import PolicyEngine

REPO_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
RESULTS_LIMIT: Final = 2 * 1024 * 1024


def demo_repos(root: Path) -> dict[str, Path]:
    """Every demo repo by exact name (plain directories only; never a path from a client)."""
    if not root.is_dir():
        return {}
    return {
        entry.name: entry
        for entry in sorted(root.iterdir())
        if entry.is_dir() and not entry.is_symlink() and REPO_NAME.fullmatch(entry.name)
    }


def policy(engine: PolicyEngine) -> dict[str, JsonValue]:
    """The global rulebook only: no learned layer, no plan."""
    rulebook = engine.rulebook
    rules: list[JsonValue] = [
        {
            "id": rule.spec.rule_id,
            "decision": rule.spec.decision.value,
            "risk": rule.spec.risk.value,
            "reason": rule.spec.reason,
            "tools": list[JsonValue](sorted(rule.tools)),
        }
        for rule in rulebook.rules
    ]
    counts = Counter(rule.spec.decision.value for rule in rulebook.rules)
    return {
        "rules": rules,
        "secret_patterns": list[JsonValue](rulebook.context.secrets.globs),
        "counts": {key: counts.get(key, 0) for key in ("deny", "ask", "allow")},
    }


def bench_scenarios() -> dict[str, JsonValue]:
    """The Attack gallery. Empty until the Bench (S8) registers its scenarios."""
    return {"total": 0, "scenarios": [], "categories": []}


def bench_scenario_ids() -> frozenset[str]:
    return frozenset()


def bench_results(path: Path) -> Mapping[str, JsonValue] | None:
    """The last Bench results file, or None when missing, too big or not an object."""
    try:
        if path.stat().st_size > RESULTS_LIMIT:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
