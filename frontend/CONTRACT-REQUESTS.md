# Contract requests from the panel (P1)

What the panel needs that `contract/` does not give it yet, why, and the exact event or
field. Each item names the **adapter** the panel uses meanwhile; the adapter is marked in
the code and lives in one place (`lib/api.ts` unless stated), so closing a request means
deleting the adapter. Where `Specs/API.md` and `contract/` disagree, the contract won.

Written 4 Nov by P1. Source of truth for the panel's event types is `contract/events.ts`
(no event type is hand-written in `frontend/`).

## A. Where API.md and the contract disagree (contract won)

| # | API.md says | Contract says | What the panel does |
| --- | --- | --- | --- |
| A1 | `approval_needed` carries flat fields (step, tool, args, rule, reason, risk, …); the banner is built in the panel from them | `approval_needed` carries one `Approval` record (eyebrow, headline, body blocks, choices, free text, timeout) with final wording | One banner component renders every kind from the record. No wording is written in the panel for banners. |
| A2 | `POST /runs/{id}/approve` takes `{step, approved, kind, plan edits?, answer?}` | "Answer with the approval's `id` and a choice `id` (plus the text for a `free_text` box)" | Sends `{approval_id, choice, answer?}`. **Please confirm these exact field names** (the contract text does not name the JSON fields). Plan edits are dropped: the panel has no plan editor (REBUILD GUIDE: build the editor or cut the API support). |
| A3 | Blocked sentences are written in the panel (`describe.ts`) | `step.blocked_sentence` is written by the backend | The panel renders the field as is. |
| A4 | Pending approval = the last event is an ask | Pending = last `approval_needed` without a matching `approval_resolved` | Implemented as the contract says (`lib/derive.ts pendingApproval`), with tests. |
| A5 | `deliver` has status and the red-team result | `deliver` has `files, excluded, blocked, released_by_human, patch_bytes` only | Red-team result is read from the `redteam` event. |
| A6 | `GET /runs/{id}/learn` for suggestions | `learn` event carries them | Read from the event; only `POST …/learn/accept` is called. |

## B. Types the contract does not define (adapters in `lib/api.ts`)

Everything below is a JSON payload with no contract type. The panel's adapter types were
written from `Specs/API.md`. Please add them to `src/sandcage/contract` so `events.ts`
carries them and the panel can drop its copies.

| # | Payload | Fields the panel reads |
| --- | --- | --- |
| B1 | Run row (`GET /runs`, `GET /runs/{id}`) | `id, repo, task, guard, status, created, finished, provider, model, steps, counts{allowed,denied,asked,injection_tagged}, tokens, tokens_reported`. **`repo`, `task` and `created` are in no event**: the report chips, the run line and the run list need them. |
| B2 | `GET /demo/status` | `access_required, paused, message, runs_left_today, runs_per_day, active_runs, max_concurrent, sandbox_runtime` |
| B3 | `GET /bench/scenarios` | `total, scenarios[{id, category, category_label, title, blurb, task}], categories[{key, label, count}]` |
| B4 | `GET /bench/results` | `provider, model, date, scenarios[{id, category, baseline{attack_succeeded, task_completed}\|null, full{…}\|null}]`. The panel shows numbers only when `provider` is not `fake`/`scripted` and `model` is set. |
| B5 | `GET /policy` | `rules[{id, decision, risk, reason, tools}], secret_patterns[], counts{deny,ask,allow}` |
| B6 | `POST /runs` body and answer | body `{repo \| scenario, fake_scenario?, task?, guard, approve_plan}`; answer `{id}` (API.md says "run id"; the panel accepts `id` or `run_id`) |
| B7 | `POST /runs/{id}/rewind` body `{checkpoint}`; `POST …/learn/accept` body `{suggestion}` | field names are guesses from API.md prose ("checkpoint id", "suggestion id") |

## C. Gaps that change what the panel can show

| # | Gap | Why it matters | Adapter / what is shown meanwhile |
| --- | --- | --- | --- |
| C1 | **A release after `done` cannot appear in the event log** (`done` is always last). | After "Release patch" the report must say "released by you" and the headline must stop saying "withheld", also after a reload. | The panel reads `released_by_human` (and `delivery_blocked`) from `GET /runs/{id}` and passes it to `outcome(events, released)`. Please add both to the run row. |
| C2 | `blocked_sentence` may be `null` on a blocked step. | Blocked rows are the demo. | Falls back to the rule's `reason`, then `Blocked by <rule_id>`. Please guarantee non-null whenever `blocked` is true. |
| C3 | No cost or price anywhere (budget has tokens only). | Report shows "honest time/cost only when the provider reported real usage". | Time (`budget.seconds`) always; tokens only when `tokens_reported`; **cost is never shown**. If wanted, add `cost_usd: number\|null` to `Budget`/`done`, null unless priced from reported usage. |
| C4 | No measured egress record. | The containment map's "0 bytes sent". | Shown only when every recorded network attempt was blocked (so nothing left). A measured per-run egress record in `report` would make it a number. |
| C5 | Trifecta legs are "ever present" booleans; no per-leg "blocked". | The card reads present / blocked / not present. | Way-out reads "blocked" when `report.capabilities` has a `network` row with `blocked > 0`; private data and untrusted content read "present" or "not present". |
| C6 | `sandbox_runtime` is only in `done`. | Live side card "Sandbox" row and the map label. | Hidden while live; shown on the report. Add it to the run row or to `understand`/`scout` if Live should show it. |
| C7 | Server clock vs browser clock. | The countdown deadline is `approval_needed.ts + timeout_seconds`, which survives reloads but trusts the two clocks. | The countdown is capped at `timeout_seconds` and floored at 0. Add `server_time` to `GET /runs/{id}` (or an SSE comment) to correct skew. |
| C8 | Countdown wording. | PAGES says "auto-deny in m:ss" for every banner, but a breaker timeout means *end* and a red-team timeout means *keep it blocked*. | The panel writes `auto-<first word of the on_timeout choice's label> in m:ss` ("auto-end", "auto-keep"). |
| C9 | Scout flags in the run header. | "Untrusted text it has read" and the "flagged: has instructions" tag. | From `scout.flags[].path` (and `done.scout_flags`). A read of a flagged file is tagged; `step.reader.contains_instructions` is not yet used (no web reader in the mock). |

## D. Notes for S7 (wiring)

- One setting: `NEXT_PUBLIC_API_URL` (build time). `/api` behind Caddy; the mock is
  `http://127.0.0.1:8001`.
- The mock enforces the same status codes the panel handles: 401/403 (code), 404, 409
  (nothing pending / busy), 413, 422, 423 (withheld), 429 (+ `Retry-After`), 503 (paused).
- CORS in dev: the API must allow the panel's exact origin (`http://127.0.0.1:3000`),
  `Content-Type`, `Last-Event-ID` and `X-Access-Code`, and expose `Retry-After`.
