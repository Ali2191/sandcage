import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Event } from "../../contract/events.ts";
import { MockServer } from "./server.ts";

let server: MockServer;
let base = "";
const CODE = "test-code-12345";

beforeAll(async () => {
  server = new MockServer({ port: 0, speed: 0.001, code: CODE, origins: ["http://127.0.0.1:3001"], heartbeatMs: 50 });
  const port = await server.listen();
  base = `http://127.0.0.1:${port}`;
});
afterAll(async () => {
  await server.close();
});

const post = (path: string, body: unknown, code: string | null = CODE): Promise<Response> =>
  fetch(`${base}${path}`, { method: "POST", headers: { "Content-Type": "application/json", ...(code ? { "X-Access-Code": code } : {}) }, body: JSON.stringify(body) });

async function start(scenario: string): Promise<string> {
  const r = await post("/runs", { repo: "invoice-calc", fake_scenario: scenario, guard: true, approve_plan: false });
  expect(r.status).toBe(202);
  return ((await r.json()) as { id: string }).id;
}

async function events(id: string, lastEventId?: number): Promise<Event[]> {
  const r = await fetch(`${base}/runs/${id}/events`, { headers: lastEventId ? { "Last-Event-ID": String(lastEventId) } : {} });
  const text = await r.text();
  return text
    .split("\n\n")
    .map((block) => block.split("\n").find((l) => l.startsWith("data: ")))
    .filter((l): l is string => l !== undefined)
    .map((l) => JSON.parse(l.slice(6)) as Event);
}

async function waitForApproval(id: string): Promise<string> {
  for (let i = 0; i < 500; i += 1) {
    const info = (await (await fetch(`${base}/runs/${id}`)).json()) as { pending_approval: string | null };
    if (info.pending_approval) return info.pending_approval;
    await new Promise((r) => setTimeout(r, 4));
  }
  throw new Error("no approval appeared");
}

async function waitForEnd(id: string): Promise<void> {
  for (let i = 0; i < 500; i += 1) {
    if (((await (await fetch(`${base}/runs/${id}`)).json()) as { status: string }).status !== "running") return;
    await new Promise((r) => setTimeout(r, 4));
  }
  throw new Error("the run did not end");
}

describe("the mock API", () => {
  it("answers health and demo status", async () => {
    expect((await fetch(`${base}/healthz`)).status).toBe(200);
    const status = await (await fetch(`${base}/demo/status`)).json();
    expect(status).toMatchObject({ access_required: true, paused: false });
  });

  it("requires the access code on changing routes, 401 when missing and 403 when wrong", async () => {
    expect((await post("/runs", { repo: "invoice-calc", guard: true, approve_plan: false }, null)).status).toBe(401);
    expect((await post("/runs", { repo: "invoice-calc", guard: true, approve_plan: false }, "nope")).status).toBe(403);
    expect((await post("/demo/unlock", {}, "nope")).status).toBe(403);
    expect((await post("/demo/unlock", {})).status).toBe(200);
  });

  it("validates a start request", async () => {
    expect((await post("/runs", { guard: true })).status).toBe(422);
    expect((await post("/runs", { repo: "invoice-calc", scenario: "x" })).status).toBe(422);
    expect((await post("/runs", { repo: "other" })).status).toBe(404);
    expect((await post("/runs", { repo: "invoice-calc", task: "" })).status).toBe(422);
  });

  it("runs the demo: plan approval, breaker, resume, done, with the contract's events", async () => {
    const id = await start("demo");
    const plan = await waitForApproval(id);
    expect(plan).toBe("plan:0");
    expect((await post(`/runs/${id}/approve`, { approval_id: "plan:0", choice: "approve" })).status).toBe(200);
    const breaker = await waitForApproval(id);
    expect(breaker).toBe("breaker:4");
    expect((await post(`/runs/${id}/approve`, { approval_id: breaker, choice: "resume" })).status).toBe(200);
    await waitForEnd(id);
    const all = await events(id);
    expect(all.at(-1)?.kind).toBe("done");
    expect(all.map((e) => e.id)).toEqual([...all.map((e) => e.id)].sort((a, b) => a - b));
    expect(all.filter((e) => e.kind === "step" && e.blocked)).toHaveLength(2);
    const resolved = all.filter((e) => e.kind === "approval_resolved");
    expect(resolved.map((e) => e.kind === "approval_resolved" && e.by)).toEqual(["human", "human"]);
  });

  it("replays from Last-Event-ID without repeating events", async () => {
    const id = await start("skip_test");
    await waitForEnd(id);
    const all = await events(id);
    const cut = all[3]?.id ?? 0;
    const rest = await events(id, cut);
    expect(rest.map((e) => e.id)).toEqual(all.filter((e) => e.id > cut).map((e) => e.id));
  });

  it("refuses an answer nothing waits for (409) and an unknown choice (422)", async () => {
    const id = await start("demo");
    await waitForApproval(id);
    expect((await post(`/runs/${id}/approve`, { approval_id: "breaker:4", choice: "resume" })).status).toBe(409);
    expect((await post(`/runs/${id}/approve`, { approval_id: "plan:0", choice: "maybe" })).status).toBe(422);
    expect((await post(`/runs/${id}/approve`, { approval_id: "plan:0" })).status).toBe(422);
    expect((await post(`/runs/${id}/stop`, {})).status).toBe(200);
  });

  it("stops in any state, once, and delivers nothing", async () => {
    const id = await start("demo");
    await waitForApproval(id);
    await post(`/runs/${id}/stop`, {});
    await post(`/runs/${id}/stop`, {});
    await waitForEnd(id);
    const all = await events(id);
    expect(all.filter((e) => e.kind === "stopped")).toHaveLength(1);
    expect(all.at(-1)).toMatchObject({ kind: "done", status: "stopped", delivered: false });
    expect((await fetch(`${base}/runs/${id}/patch`)).status).toBe(409);
  });

  it("withholds a patch with 423 until a human releases it, and release is once", async () => {
    const id = await start("sneaky_urllib");
    expect(await waitForApproval(id)).toBe("action:3");
    await post(`/runs/${id}/approve`, { approval_id: "action:3", choice: "allow" });
    expect(await waitForApproval(id)).toBe("redteam:0");
    await post(`/runs/${id}/approve`, { approval_id: "redteam:0", choice: "keep" });
    await waitForEnd(id);
    expect((await fetch(`${base}/runs/${id}/patch`)).status).toBe(423);
    expect((await post(`/runs/${id}/release`, {})).status).toBe(200);
    expect((await fetch(`${base}/runs/${id}/patch`)).status).toBe(200);
    expect((await post(`/runs/${id}/release`, {})).status).toBe(409);
  });

  it("does not leak ids into paths: an odd run id is a 404", async () => {
    expect((await fetch(`${base}/runs/..%2Fetc`)).status).toBe(404);
    expect((await fetch(`${base}/runs/zzzzzzzzzzzz`)).status).toBe(404);
  });

  it("answers CORS preflight only for the panel's origin", async () => {
    const ok = await fetch(`${base}/runs`, { method: "OPTIONS", headers: { Origin: "http://127.0.0.1:3001" } });
    expect(ok.headers.get("access-control-allow-origin")).toBe("http://127.0.0.1:3001");
    const bad = await fetch(`${base}/runs`, { method: "OPTIONS", headers: { Origin: "http://evil.example" } });
    expect(bad.headers.get("access-control-allow-origin")).toBeNull();
  });

  it("serves the real rulebook counts", async () => {
    const policy = (await (await fetch(`${base}/policy`)).json()) as { counts: Record<string, number>; rules: unknown[] };
    expect(policy.counts).toEqual({ deny: 14, ask: 5, allow: 6 });
    expect(policy.rules).toHaveLength(25);
  });

  it("lists 21 scenarios in 10 categories", async () => {
    const cat = (await (await fetch(`${base}/bench/scenarios`)).json()) as { total: number; categories: { count: number }[] };
    expect(cat.total).toBe(21);
    expect(cat.categories).toHaveLength(10);
    expect(cat.categories.reduce((n, c) => n + c.count, 0)).toBe(21);
  });
});
