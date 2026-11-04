"""Read-only catalogue data: demo repos, the rulebook, Bench scenarios and results.

The scenario list is the Bench catalogue; the results are absent (404) until a Bench run
writes its file, and nothing is invented to fill the gallery or the results page.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from sandcage.bench.catalogue import find, scenario_ids, scenarios
from sandcage.bench.scenario import Scenario
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
    """The Attack gallery: every registered Bench scenario, with its hand-written title and
    one-line story (``blurb``), and the categories with their counts, in catalogue order."""
    rows: list[JsonValue] = []
    counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for scenario in scenarios():
        counts[scenario.category] += 1
        labels.setdefault(scenario.category, scenario.category_label)
        rows.append(
            {
                "id": scenario.id,
                "category": scenario.category,
                "category_label": scenario.category_label,
                "title": scenario.title,
                "blurb": scenario.story,
                "task": scenario.task,
                "channel": scenario.channel.value,
                "goal": scenario.goal.value,
                "benign": scenario.benign,
            }
        )
    categories: list[JsonValue] = [
        {"key": key, "label": labels[key], "count": count} for key, count in counts.items()
    ]
    return {"total": len(rows), "scenarios": rows, "categories": categories}


def bench_scenario(scenario_id: str) -> Scenario | None:
    """A registered scenario by its exact id (never a path or a pattern)."""
    return find(scenario_id)


def bench_scenario_ids() -> frozenset[str]:
    return scenario_ids()


def bench_results(path: Path) -> Mapping[str, JsonValue] | None:
    """The last Bench results file, or None when missing, too big or not an object."""
    try:
        if path.stat().st_size > RESULTS_LIMIT:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
