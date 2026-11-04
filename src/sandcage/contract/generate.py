"""Render the generated contract files: JSON Schema, TypeScript types, the sample stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from sandcage.contract.approval import ApprovalKind
from sandcage.contract.events import EVENT_ADAPTER, EVENT_KINDS, RunStatus, dump_event
from sandcage.contract.sample import demo_events
from sandcage.contract.typescript import to_typescript
from sandcage.report.report import declined_line, headline, outcome

DEFAULT_OUT: Final = Path(__file__).resolve().parents[3] / "contract"
SCHEMA_FILE: Final = "events.schema.json"
TYPESCRIPT_FILE: Final = "events.ts"
SAMPLE_FILE: Final = "samples/demo-run.jsonl"
WORDING_FILE: Final = "samples/report-wording.json"
_WORDING_MAX_COUNT: Final = 3


def event_schema() -> dict[str, object]:
    schema: dict[str, object] = EVENT_ADAPTER.json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Event"
    schema["description"] = "Every event a Sandcage run can emit (generated from Python)."
    return schema


def report_wording() -> dict[str, object]:
    """The report's outcome pill, headline and declined line for every case, written by the
    backend's own functions, so a mock server looks wording up instead of writing it."""
    rows = [
        {
            "status": status.value,
            "withheld": withheld,
            "blocked": blocked,
            "outcome": outcome(status, withheld=withheld),
            "headline": headline(status, blocked, withheld=withheld),
        }
        for status in RunStatus
        for withheld in (False, True)
        for blocked in range(_WORDING_MAX_COUNT + 1)
    ]
    declined = [
        {"declined": count, "declined_line": declined_line(count)}
        for count in range(_WORDING_MAX_COUNT + 1)
    ]
    return {"reports": rows, "declined": declined}


def render() -> dict[str, str]:
    """Every generated file, by path relative to the contract folder."""
    schema = event_schema()
    typescript = to_typescript(
        schema,
        root_name="Event",
        constants={
            "EVENT_KINDS": EVENT_KINDS,
            "APPROVAL_KINDS": tuple(kind.value for kind in ApprovalKind),
        },
    )
    sample = "".join(
        json.dumps(dump_event(event), sort_keys=True, ensure_ascii=False) + "\n"
        for event in demo_events()
    )
    return {
        SCHEMA_FILE: json.dumps(schema, indent=2, sort_keys=True) + "\n",
        TYPESCRIPT_FILE: typescript,
        SAMPLE_FILE: sample,
        WORDING_FILE: json.dumps(report_wording(), indent=1, sort_keys=True) + "\n",
    }


def write_files(out: str | Path) -> list[str]:
    root = Path(out)
    written = []
    for name, text in render().items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(name)
    return written


def stale_files(out: str | Path) -> list[str]:
    """Generated files that are missing or differ from what the models produce now."""
    root = Path(out)
    stale = []
    for name, text in render().items():
        target = root / name
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current != text:
            stale.append(name)
    return stale
