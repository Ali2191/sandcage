/**
 * The mock must not drift from the real API. Every event it serves is checked against
 * the contract's JSON Schema (`contract/events.schema.json`): the kind is known and the
 * keys are exactly the contract's keys. Every other payload is checked against the key
 * lists in `contract/API.md`. (Compile time also helps: the mock builds these payloads
 * with the panel's own API types.)
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { MockServer } from "./server.ts";

interface SchemaDef {
  properties?: Record<string, { const?: string }>;
}
interface Schema {
  $defs: Record<string, SchemaDef>;
}

const SCHEMA = JSON.parse(readFileSync(path.join(import.meta.dirname, "../../contract/events.schema.json"), "utf8")) as Schema;

/** kind -> the keys the contract says that event has. */
const EVENT_KEYS = new Map<string, string[]>();
for (const def of Object.values(SCHEMA.$defs)) {
  const kind = def.properties?.["kind"]?.const;
  if (kind !== undefined && def.properties) EVENT_KEYS.set(kind, Object.keys(def.properties).sort());
}

const RUN_ROW_KEYS = [
  "id", "repo", "task", "guard", "status", "created", "finished", "provider", "model", "sandbox_runtime", "steps", "counts",
  "tokens", "tokens_reported", "outcome", "delivered", "delivery_blocked", "released_by_human",
].sort();
const RUN_DETAIL_KEYS = [...RUN_ROW_KEYS, "pending", "result", "server_time"].sort();
const DEMO_STATUS_KEYS = [
  "access_required", "public", "paused", "message", "active_runs", "max_concurrent", "runs_today", "runs_per_day", "runs_left_today",
  "runs_per_client_per_hour", "tokens_today", "daily_token_cap", "run_token_cap", "resets_at", "sandbox_runtime", "provider_ready",
].sort();
const SANDBOX_RUNTIME_KEYS = ["requested", "active", "gvisor", "label", "reason"].sort();

let server: MockServer;
let base = "";

beforeAll(async () => {
  server = new MockServer({ port: 0, speed: 0.001, code: null, origins: [], heartbeatMs: 50 });
  base = `http://127.0.0.1:${await server.listen()}`;
  const seeded = await fetch(`${base}/__mock/seed`, { method: "POST" });
  expect(seeded.status).toBe(200);
}, 60_000);
afterAll(async () => {
  await server.close();
});

const get = async (route: string): Promise<unknown> => (await fetch(`${base}${route}`)).json();
const keys = (value: unknown): string[] => Object.keys(value as Record<string, unknown>).sort();

describe("mock payloads match contract/API.md", () => {
  it("serves the repo list as {repos}", async () => {
    expect(keys(await get("/repos"))).toEqual(["repos"]);
  });

  it("serves the demo status with every field", async () => {
    const status = (await get("/demo/status")) as { sandbox_runtime: unknown };
    expect(keys(status)).toEqual(DEMO_STATUS_KEYS);
    expect(keys(status.sandbox_runtime)).toEqual(SANDBOX_RUNTIME_KEYS);
  });

  it("serves run rows and the run detail", async () => {
    const list = (await get("/runs?limit=100")) as { runs: { id: string }[] };
    expect(list.runs.length).toBeGreaterThan(0);
    for (const row of list.runs) expect(keys(row)).toEqual(RUN_ROW_KEYS);
    const detail = await get(`/runs/${list.runs[0]?.id}`);
    expect(keys(detail)).toEqual(RUN_DETAIL_KEYS);
  });
});

describe("mock events match contract/events.schema.json", () => {
  it("every event of every seeded scenario has exactly its contract's keys", async () => {
    const list = (await get("/runs?limit=100")) as { runs: { id: string }[] };
    const seen = new Set<string>();
    for (const { id } of list.runs) {
      const text = await (await fetch(`${base}/runs/${id}/events`)).text();
      const events = text
        .split("\n\n")
        .map((block) => block.split("\n").find((l) => l.startsWith("data: ")))
        .filter((l): l is string => l !== undefined)
        .map((l) => JSON.parse(l.slice(6)) as Record<string, unknown>);
      for (const event of events) {
        const kind = String(event["kind"]);
        const expected = EVENT_KEYS.get(kind);
        expect(expected, `unknown event kind ${kind}`).toBeDefined();
        expect(keys(event), `keys of a ${kind} event`).toEqual(expected);
        seen.add(kind);
      }
    }
    for (const kind of ["report", "done", "approval_needed", "step", "deliver"]) expect(seen.has(kind)).toBe(true);
  }, 60_000);
});
