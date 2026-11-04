# Sandcage HTTP API (as built in S6)

The shapes below are what `src/sandcage/api/` serves today. Event payloads are the
contract types in `events.ts` / `events.schema.json`; this file covers everything else.
Start it with `python -m sandcage.api` (or `make api`).

## Basics

- JSON in and out, UTF-8. Every response closes the connection.
- **Errors** are `{"detail": "<message fit to show>"}` with the status below. `429` and
  wrong-code lockouts carry `Retry-After` (seconds).
- **Access code**: header `X-Access-Code`. Required on **every `POST`** when a code is
  configured; `GET` is always open. Missing → `401`, wrong → `403`, 10 wrong codes in
  10 minutes from one client → `429` (even with the right code) until the window passes.
  No code configured (local mode) → open.
- **Body limit** 64 KB: over it → `413` before the body is read; `Transfer-Encoding`
  (chunked) → `411`; a body shorter than its `Content-Length` → `400`; bad JSON → `400`;
  a body that does not fit its schema (unknown field, wrong type, too long) → `422`.
- **Run ids** are 12 lowercase hex characters. Any other id → `404` before any lookup.
- **CORS**: only for the exact origin in `PANEL_ORIGIN` (e.g. `http://127.0.0.1:3000`):
  methods `GET, POST`; request headers `Content-Type, Last-Event-ID, X-Access-Code`;
  exposed `Retry-After`. No `PANEL_ORIGIN` → no CORS headers (same-origin behind Caddy at
  `/api`).

## Health and demo

| Route | Answer |
|---|---|
| `GET /healthz` | `{"ok": true}` |
| `GET /readyz` | `200` or `503` with `{"ready": bool, "checks": {name: {"ok": bool, "detail": str, ...}}}`. Checks: `docker`, `runtime` (+ `active`: `"runsc"` / `"runc"` / `null`, `label`, `gvisor`), `disk` (+ `free_bytes`, needs 1 GiB), `provider`. |
| `GET /demo/status` | `{access_required, public, paused, message (null unless paused), active_runs, max_concurrent, runs_today, runs_per_day, runs_left_today, runs_per_client_per_hour, tokens_today, daily_token_cap, run_token_cap, resets_at (unix s, next UTC midnight), sandbox_runtime: {requested, active, gvisor, label, reason} \| null, provider_ready}` |
| `POST /demo/unlock` | `{"ok": true}` when the code is right (401/403/429 otherwise) |

## Catalogue (read-only)

| Route | Answer |
|---|---|
| `GET /repos` | `{"repos": ["invoice-calc"]}` (exact names; only these are accepted) |
| `GET /policy` | `{"rules": [{id, decision, risk, reason, tools}], "secret_patterns": [str], "counts": {deny, ask, allow}}` (rulebook only) |
| `GET /bench/scenarios` | `{"total": 0, "scenarios": [], "categories": []}` until the Bench (S8) exists |
| `GET /bench/results` | the Bench results file as written, or `404` until one exists |

## Runs

### `POST /runs` → `202 {"id": "<run id>", "run_id": "<run id>"}`

```json
{"repo": "invoice-calc", "task": "Fix the failing test.", "guard": true,
 "approve_plan": false, "done_criteria": ["..."], "fake_scenario": "demo"}
```

- Exactly one of `repo` / `scenario` (`422` otherwise). `scenario` → `404` until S8.
- `task` 1–2000 characters. `done_criteria` up to 8, each up to 200.
- `guard: false` runs in observe mode (the gate records, every action runs).
- `fake_scenario` (`demo`, `skip_test`, `rollback`, `sneaky_urllib`, `clarify`) only with
  the scripted provider (`422` otherwise).
- Order of checks: kill switch (`503` + friendly message), access code, body, repo
  (`404`), provider configured (`503`), then atomically: slots (`409`, message names the
  running ids), demo limits (`429` + `Retry-After`), run row.

### Run row (`GET /runs` items, and the base of `GET /runs/{id}`)

```json
{"id": "...", "repo": "invoice-calc", "task": "...", "guard": true,
 "status": "running|finished|stopped|interrupted|...", "created": 1793000000.0,
 "finished": null, "provider": "scripted", "model": null, "sandbox_runtime": null,
 "steps": 9, "counts": {"allowed": 6, "denied": 2, "asked": 1, "injection_tagged": 2},
 "tokens": 1234, "tokens_reported": false, "outcome": "Delivered|Withheld|Stopped by you|<status>|null",
 "delivered": true, "delivery_blocked": false, "released_by_human": false}
```

`released_by_human` is true when the human released the patch at the red-team question
during the run **or** with `POST /runs/{id}/release` after `done` (a release after `done`
cannot appear in the event log). `sandbox_runtime` and `outcome` are filled once the run
ends. No cost anywhere; `tokens_reported` says whether `tokens` was provider-reported or
estimated.

| Route | Answer | Errors |
|---|---|---|
| `GET /runs?limit=20&offset=0` | `{"runs": [row], "total", "limit", "offset"}`, newest first; limit 1–100 | `422` |
| `GET /runs/{id}` | row + `"pending": Approval \| null` (the open question, the same record as in `approval_needed`), `"result": DoneEvent \| null`, `"server_time": unix s` | `404` |
| `POST /runs/{id}/stop` | `{"id", "status": "stopped" \| "stopping" \| <final status>}`. Stopping twice records one `stopped` event; stopping a finished run changes nothing. | `404` |
| `POST /runs/{id}/approve` | body `{"approval_id": str, "choice": str, "answer"?: str}` → `{"id", "approval_id", "choice", "approved"}`. `answer` only for an approval with `free_text` (max 1000 after trimming). | `404`; `409` nothing open with that id (already answered, timed out, stopped); `422` unknown choice, missing/extra answer |
| `GET /runs/{id}/checkpoints` | `{"checkpoints": [{"id", "step", "files"}]}` (0 = the original) | `404` |
| `POST /runs/{id}/rewind` | body `{"checkpoint": int}` → `202 {"state": "requested", "checkpoint"}`; applied before the agent's next step (a `checkpoint` event with `restored_by: "human"` follows) | `404`; `409` not acting; `422` unknown checkpoint |

### Deliverables

| Route | Answer | Errors |
|---|---|---|
| `GET /runs/{id}/patch` | `text/x-diff`, `Content-Disposition: attachment; filename="sandcage.patch"` | `404`; `409` nothing delivered (yet); `423` withheld until released |
| `GET /runs/{id}/summary` | `text/markdown` SUMMARY.md (also while withheld) | `404`; `409` |
| `GET /runs/{id}/report?format=json\|md` | the report object (`outcome`, `headline`, `declined_line`, `blocks`, `declined`, `trifecta`, `egress`, …) or REPORT.md | `404`; `409`; `422` bad format |
| `POST /runs/{id}/release` | `{"id", "released_by_human": true}` | `404`; `409` nothing withheld |
| `GET /runs/{id}/learn` | `{"suggestions": [LearnSuggestion], "notes": [str]}` | `404` |
| `POST /runs/{id}/learn/accept` | body `{"suggestion": "<suggestion id>"}` → `{"rule_id", "file": "learned.yaml"}` | `404` unknown run/suggestion; `409` already learned; `422` invalid rule |

## Event stream: `GET /runs/{id}/events`

`text/event-stream`. Resume with the `Last-Event-ID` header (or `?after=<id>`); `422`
when it is not a number, `404` for an unknown run.

```
retry: 2000

id: 41
event: step
data: {"kind": "step", "id": 41, "run_id": "...", ...}

: ping
```

- Everything stored after the resume id is replayed in order, then new events arrive
  as they are stored (the server waits on the store; no polling).
- `: ping` comment after 15 s without events.
- The stream ends right after the `done` event, or at once when resumed at or after it.
  Interrupted runs get a `done` at API startup, so their streams end too.
- `id` is the store's event id: strictly increasing per run, never reused. Drop duplicates
  by id after a reconnect.
- The pending approval is the last `approval_needed` without a matching
  `approval_resolved`; after a reload, `GET /runs/{id}` gives it as `pending`.

## Settings (environment)

| Name | Meaning |
|---|---|
| `DEMO_MODE` | `local` (default) or `public`; public refuses to start without `DEMO_ACCESS_CODE` of 12+ characters |
| `DEMO_ACCESS_CODE` | the shared code (never logged or echoed) |
| `DEMO_PAUSED` | kill switch: `true` refuses new runs with `503` |
| `DEMO_MAX_CONCURRENT_RUNS` (2), `DEMO_RUNS_PER_IP_PER_HOUR` (3), `DEMO_RUNS_PER_DAY` (40), `DEMO_RUN_TOKEN_CAP` (60000), `DEMO_DAY_TOKEN_CAP` (2000000) | limits |
| `DEMO_TRUST_PROXY` | `true` only behind our own proxy: client = last `X-Forwarded-For` hop |
| `PANEL_ORIGIN` | the one CORS origin (unset behind Caddy) |
| `LLM_PROVIDER` | `scripted` (default), `vultr` or `openai`; `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` |
| `SANDCAGE_DATA_DIR` (`data`), `SANDCAGE_DB` (`<data>/sandcage.db`) | event log and `learned.yaml`; one API process per log (lock file) |
| `SANDCAGE_API_HOST` (`127.0.0.1`), `SANDCAGE_API_PORT` (8000) | where it listens |
| `SANDBOX_RUNTIME` | `runc` (default) or `runsc` (trusted only after the gVisor banner check) |
| `SANDCAGE_BENCH_RESULTS` | the Bench results file |
