"""The typed event contract: models, tolerant parsing, generated files, the sample stream."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandcage.contract import EVENT_KINDS, EventParseError, UnknownEvent, dump_event, parse_event
from sandcage.contract.approval import ApprovalKind
from sandcage.contract.events import (
    EVENT_ADAPTER,
    ApprovalNeededEvent,
    ApprovalResolvedEvent,
    DoneEvent,
    StepEvent,
)
from sandcage.contract.generate import DEFAULT_OUT, SAMPLE_FILE, render, stale_files, write_files
from sandcage.contract.sample import demo_events
from sandcage.contract.typescript import SchemaTranslationError, to_typescript


def test_event_kinds_match_the_union() -> None:
    schema = EVENT_ADAPTER.json_schema(mode="serialization")
    mapping = schema["discriminator"]["mapping"]
    assert sorted(mapping) == sorted(EVENT_KINDS)
    assert len(set(EVENT_KINDS)) == len(EVENT_KINDS)


def test_generated_files_are_up_to_date() -> None:
    assert stale_files(DEFAULT_OUT) == [], "run `make contract` and commit contract/"


def test_check_reports_missing_and_changed_files(tmp_path: Path) -> None:
    assert sorted(stale_files(tmp_path)) == sorted(render())
    write_files(tmp_path)
    assert stale_files(tmp_path) == []
    (tmp_path / "events.ts").write_text("// edited by hand\n", encoding="utf-8")
    assert stale_files(tmp_path) == ["events.ts"]


def test_rendering_is_deterministic() -> None:
    assert render() == render()


# --- tolerant parsing --------------------------------------------------------------------


def _step_payload() -> dict[str, object]:
    step = next(e for e in demo_events() if isinstance(e, StepEvent))
    return dict(dump_event(step))


def test_every_sample_event_round_trips() -> None:
    for event in demo_events():
        assert parse_event(dump_event(event)) == event


def test_unknown_fields_are_ignored() -> None:
    payload = _step_payload() | {"added_in_a_later_version": {"x": 1}}
    event = parse_event(payload)
    assert isinstance(event, StepEvent)
    assert "added_in_a_later_version" not in dump_event(event)


def test_unknown_kind_is_kept_not_dropped() -> None:
    event = parse_event({"kind": "telemetry", "id": 9, "run_id": "abc", "x": [1]})
    assert isinstance(event, UnknownEvent)
    assert (event.kind, event.id, event.run_id) == ("telemetry", 9, "abc")
    assert event.raw["x"] == [1]


def test_unknown_kind_with_odd_id_falls_back() -> None:
    event = parse_event({"kind": "new", "id": True, "run_id": 5})
    assert isinstance(event, UnknownEvent)
    assert (event.id, event.run_id) == (0, "")


@pytest.mark.parametrize("raw", [None, [], "step", {"no": "kind"}, {"kind": 3}])
def test_non_events_are_rejected(raw: object) -> None:
    with pytest.raises(EventParseError):
        parse_event(raw)


def test_known_kind_that_does_not_fit_is_an_error() -> None:
    payload = _step_payload()
    payload["decision"] = "maybe"
    with pytest.raises(EventParseError, match="'step' event does not fit"):
        parse_event(payload)
    del payload["decision"]
    with pytest.raises(EventParseError):
        parse_event(payload)


def test_events_are_frozen() -> None:
    event = demo_events()[0]
    with pytest.raises(ValueError, match="frozen"):
        event.run_id = "other"  # type: ignore[misc]


# --- the sample stream --------------------------------------------------------------------


def test_sample_file_lines_parse_as_events() -> None:
    lines = (DEFAULT_OUT / SAMPLE_FILE).read_text(encoding="utf-8").splitlines()
    parsed = [parse_event(json.loads(line)) for line in lines]
    assert not any(isinstance(event, UnknownEvent) for event in parsed)
    assert [event.id for event in parsed] == list(range(1, len(parsed) + 1))


def test_sample_is_the_demo_story() -> None:
    events = demo_events()
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].status == "finished"
    times = [event.ts for event in events]
    assert times == sorted(times)
    blocked = [e for e in events if isinstance(e, StepEvent) and e.blocked]
    assert [e.blocked_sentence for e in blocked] == [
        "Tried to send your .env to collector.example.net",
        "Tried to read your secrets in .env",
    ]
    assert [e.rule_id for e in blocked] == ["net.deny_tools", "path.deny_secrets"]
    for step in blocked:
        assert step.tags == ["prompt_injection_suspected"]
        assert step.source == "README.md"
    allowed = [e for e in events if isinstance(e, StepEvent) and not e.blocked]
    assert all(e.blocked_sentence is None and e.executed for e in allowed)


def test_every_sample_approval_is_resolved_with_its_id() -> None:
    events = demo_events()
    asked = [e for e in events if isinstance(e, ApprovalNeededEvent)]
    answered = [e for e in events if isinstance(e, ApprovalResolvedEvent)]
    assert [a.approval.id for a in asked] == [r.approval_id for r in answered]
    assert {a.approval.kind for a in asked} == {ApprovalKind.PLAN, ApprovalKind.BREAKER}


def test_sample_holds_no_secret_value() -> None:
    text = (DEFAULT_OUT / SAMPLE_FILE).read_text(encoding="utf-8")
    assert "sk_live" not in text


# --- TypeScript generation -----------------------------------------------------------------


def test_typescript_declares_every_event_and_the_constants() -> None:
    text = render()["events.ts"]
    for name in ("StepEvent", "DoneEvent", "Approval", "ApprovalNeededEvent", "Budget"):
        assert f"export interface {name} {{" in text
    assert 'export type Decision = "allow" | "ask" | "deny";' in text
    assert "export type Event = UnderstandEvent | ScoutEvent" in text
    assert 'export type EventKind = Event["kind"];' in text
    assert 'export const EVENT_KINDS = ["understand", "scout", ' in text
    assert 'export const APPROVAL_KINDS = ["action", "plan", "amendment", ' in text
    assert '  kind: "step";' in text
    assert "  body: (CodeBlock | TextBlock | FactsBlock | FindingsBlock)[];" in text


def _ts(schema: dict[str, object]) -> str:
    return to_typescript(
        {"$defs": {"Thing": schema}, "$ref": "#/$defs/Thing"}, root_name="Root", constants={}
    )


def test_typescript_rejects_constructs_it_does_not_know() -> None:
    with pytest.raises(SchemaTranslationError):
        _ts({"type": "string", "format": "date-time", "pattern": "x", "allOf": []})
    with pytest.raises(SchemaTranslationError):
        _ts({"$ref": "https://example.com/other"})
    with pytest.raises(SchemaTranslationError):
        _ts({"anyOf": []})


def test_typescript_doc_comments_cannot_be_closed_early() -> None:
    text = _ts({"type": "object", "properties": {}, "description": "ends */ here"})
    assert "ends *\\/ here" in text
    assert "ends */ here" not in text


def test_typescript_optional_and_maps() -> None:
    text = _ts(
        {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "object", "additionalProperties": {"type": "string"}},
                "c": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            },
            "required": ["a"],
        }
    )
    assert "  a: number;" in text
    assert "  b?: { [key: string]: string };" in text
    assert "  c?: (string | null)[];" in text
