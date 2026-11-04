# The Sandcage contract

One typed definition of every event a run emits and of the one approval record every
human question uses. The backend writes events only through these models; the panel
reads them through the generated types. Nothing in this folder is edited by hand except
this README.

| File | What it is |
| --- | --- |
| `events.schema.json` | JSON Schema (draft 2020-12) of `Event`, a union discriminated by `kind` |
| `events.ts` | TypeScript types for the panel: every event interface, `Event`, `EventKind`, `Approval`, the enums, and the `EVENT_KINDS` / `APPROVAL_KINDS` constants |
| `samples/demo-run.jsonl` | The scripted demo run, one event per line in stream order, for the panel's mock API to replay |

Source of truth: `src/sandcage/contract/` (`events.py`, `approval.py`, `common.py`).

## Regenerate

```
make contract
```

`make check` runs `make contract-check`, which fails when any generated file differs from
what the Python models produce, so a model change without regenerated files cannot land.

## Rules the panel can rely on

- **Envelope.** Every event has `id` (ordered, also the SSE id), `kind`, `run_id`,
  `phase`, `ts` (Unix seconds) and `budget` (a snapshot or `null`). Every field in the
  types is always present in what the backend writes (optional values are `null`).
- **Forward compatibility.** Ignore fields you don't know. Keep, but don't render, an
  event whose `kind` is not in `EVENT_KINDS` (the Python reader does the same:
  `parse_event` returns `UnknownEvent`).
- **Approvals.** `approval_needed` carries an `Approval`: `eyebrow`, `headline`, `body`
  (blocks of type `code`, `text`, `facts`, `findings`), an optional `note`, `choices`
  (label, `primary`, `shortcut`, and `approves`), an optional `free_text` box, `layout`,
  `timeout_seconds`, `on_timeout` (the choice a timeout picks) and `timeout_text`.
  Render every banner from this record; the wording is already final (Design/PAGES.md
  section 6). Answer with the approval's `id` and a choice `id` (plus the text for a
  `free_text` box). `approval_resolved` closes it, whoever answered (human, timeout,
  stop, error). The pending approval is the last `approval_needed` without a matching
  `approval_resolved`.
- **Blocked steps.** A `step` with `blocked: true` was denied, or asked and not
  approved. `blocked_sentence` is Sandcage's own sentence for it ("Tried to send your
  .env to collector.example.net"), written from the action and the rule, never from the
  agent's words. Use it as is.
- **Honest numbers.** `budget.tokens_reported` is false when any token count was
  estimated; don't show estimated usage or cost as measured.
- `done` is always the last event of a run.

## About the sample stream

`samples/demo-run.jsonl` is built from the contract models by
`src/sandcage/contract/sample.py`. The gate verdicts, rule ids, reasons, taint tags and
blocked sentences in it come from the real rulebook and wording code; the other events
(understand, scout, plan, verify, red-team, deliver, report, learn) are assembled to
match the demo walkthrough, because the harness that emits them is built later. Its
token counts are marked as estimated. Once the harness runs the scripted demo end to
end, a recorded stream of the same shape replaces it.
