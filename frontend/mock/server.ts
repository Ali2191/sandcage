/**
 * The mock Sandcage API: the endpoint shapes of Specs/API.md, with events from the typed
 * contract. Scripted runs (scenarios.ts) answer pause, approve, deny, stop, rewind and
 * release for real. Loading, empty and error states are injected through /__mock/*.
 * Standard library only. Start it with `npm run mock`.
 */
import { readFileSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import type { Event } from "../../contract/events.ts";
import type { DemoStatus, RunDetail, RunRow } from "../lib/api.ts";
import { doneDraft } from "./builders.ts";
import { categories, scenarios } from "./catalogue.ts";
import { MockRun, StopSignal, type RunMeta } from "./run.ts";
import { reportMarkdown, runScenario, SCENARIO_IDS, type ScenarioId } from "./scenarios.ts";

export interface MockOptions {
  port: number;
  host: string;
  /** Browser origins allowed to call the API. */
  origins: string[];
  /** 1 = readable pace; 0.02 = fast for tests. */
  speed: number;
  /** When set, changing routes need this access code. */
  code: string | null;
  runsPerDay: number | null;
  heartbeatMs: number;
}

export const DEFAULT_OPTIONS: MockOptions = {
  port: 8001,
  host: "127.0.0.1",
  origins: ["http://127.0.0.1:3000", "http://127.0.0.1:3001"],
  speed: 1,
  code: null,
  runsPerDay: null,
  heartbeatMs: 15_000,
};

type Target = "repos" | "runs" | "run" | "gallery" | "bench" | "policy" | "demo" | "patch" | "report";
interface Injection {
  mode: "error" | "empty" | "slow";
  ms?: number;
}

const policyDoc: unknown = JSON.parse(readFileSync(path.join(import.meta.dirname, "data/policy.json"), "utf8"));

function runRow(run: MockRun): RunRow {
  const done = run.events.find((e) => e.kind === "done");
  const steps = run.events.filter((e) => e.kind === "step");
  return {
    id: run.meta.id,
    repo: run.meta.repo,
    task: run.meta.task,
    guard: run.meta.guard,
    status: run.status,
    created: run.meta.created,
    finished: done ? done.ts : null,
    provider: "fake",
    model: "scripted",
    steps: steps.length,
    counts: done && done.kind === "done" ? done.counts : null,
    tokens: null,
    tokens_reported: false,
    outcome: done && done.kind === "done" ? done.outcome : null,
    delivered: done && done.kind === "done" ? done.delivered || run.outputs.released : false,
    sandbox_runtime: done && done.kind === "done" ? done.sandbox_runtime : null,
    released_by_human: run.outputs.released,
    delivery_blocked: run.outputs.blocked,
  };
}

function runDetail(run: MockRun): RunDetail {
  const done = run.events.find((e) => e.kind === "done");
  return {
    ...runRow(run),
    pending: run.pendingApproval,
    result: done && done.kind === "done" ? done : null,
    server_time: Date.now() / 1000,
  };
}

/** The scripted Bench record the backend wrote (`make bench`), served as the API serves it. */
function scriptedRecord(): unknown {
  return JSON.parse(readFileSync(path.join(import.meta.dirname, "../../benchmarks/out/results.json"), "utf-8"));
}

export class MockServer {
  readonly options: MockOptions;
  private readonly runs = new Map<string, MockRun>();
  private order: string[] = [];
  private injections = new Map<Target, Injection>();
  private paused = false;
  private benchResults: unknown = null;
  private eventSeq = 0;
  private runsToday = 0;
  private server: http.Server | null = null;

  constructor(options: Partial<MockOptions> = {}) {
    this.options = { ...DEFAULT_OPTIONS, ...options };
  }

  async listen(): Promise<number> {
    const server = http.createServer((req, res) => {
      this.handle(req, res).catch((cause: unknown) => {
        if (!res.headersSent) this.send(req, res, 500, { detail: "The mock API hit a problem." });
        else res.end();
        console.error("mock error", cause);
      });
    });
    this.server = server;
    await new Promise<void>((resolve) => server.listen(this.options.port, this.options.host, resolve));
    const address = server.address();
    return typeof address === "object" && address ? address.port : this.options.port;
  }

  async close(): Promise<void> {
    for (const run of this.runs.values()) run.requestStop();
    this.server?.closeAllConnections();
    await new Promise<void>((resolve) => (this.server ? this.server.close(() => resolve()) : resolve()));
  }

  // --- runs ---------------------------------------------------------------

  private startRun(input: { repo?: string; scenario?: string; task?: string; guard: boolean; approve_plan: boolean }, fake?: ScenarioId): MockRun {
    const id = Array.from({ length: 12 }, () => "0123456789abcdef"[Math.floor(Math.random() * 16)]).join("");
    const benchId = input.scenario;
    const scenario: ScenarioId = fake ?? "demo";
    const meta: RunMeta = {
      id,
      repo: benchId ? `bench:${benchId}` : (input.repo ?? "invoice-calc"),
      task: input.task ?? "Set up the project following the README, then fix the failing test.",
      guard: input.guard,
      approvePlan: input.approve_plan,
      scenario,
      created: Date.now() / 1000,
    };
    const run = new MockRun(meta, () => (this.eventSeq += 1), this.options.speed);
    this.runs.set(id, run);
    this.order.unshift(id);
    this.runsToday += 1;
    void this.drive(run, scenario);
    return run;
  }

  private async drive(run: MockRun, scenario: ScenarioId): Promise<void> {
    try {
      await runScenario({ run, scenario, approvePlan: run.meta.approvePlan });
    } catch (cause) {
      if (cause instanceof StopSignal) {
        run.recordStop(doneDraft({ status: "stopped", summary: "You stopped this run. Nothing was delivered.", guard: run.meta.guard }));
        return;
      }
      console.error("scenario failed", cause);
      run.forceDone(doneDraft({ status: "error", summary: "The mock scenario failed." }));
    }
  }

  /** Create finished runs for the lists and reports (used by `npm run mock -- --seed`). */
  async seed(): Promise<void> {
    const make = async (scenario: ScenarioId, answers: Record<string, string>): Promise<void> => {
      const run = this.startRun({ repo: "invoice-calc", guard: true, approve_plan: false }, scenario);
      const answerLoop = setInterval(() => {
        const id = run.pendingApprovalId;
        if (id) run.answer(id, answers[id.split(":")[0] ?? ""] ?? "deny", "Percentage off: 10 means 10%.");
      }, 1);
      while (!run.finished) await new Promise((r) => setTimeout(r, 5));
      clearInterval(answerLoop);
    };
    await make("demo", { plan: "approve", breaker: "resume" });
    await make("skip_test", {});
    await make("sneaky_urllib", { action: "allow", redteam: "keep" });
    await make("rollback", {});
  }

  // --- http -----------------------------------------------------------------

  private cors(req: http.IncomingMessage, res: http.ServerResponse): void {
    const origin = req.headers.origin;
    if (origin && this.options.origins.includes(origin)) {
      res.setHeader("Access-Control-Allow-Origin", origin);
      res.setHeader("Vary", "Origin");
      res.setHeader("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID, X-Access-Code");
      res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
      res.setHeader("Access-Control-Expose-Headers", "Retry-After");
    }
  }

  private send(req: http.IncomingMessage, res: http.ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
    this.cors(req, res);
    const text = typeof body === "string" ? body : JSON.stringify(body);
    res.writeHead(status, { "Content-Type": typeof body === "string" ? "text/plain; charset=utf-8" : "application/json", "Cache-Control": "no-store", ...headers });
    res.end(text);
  }

  private async readBody(req: http.IncomingMessage): Promise<Record<string, unknown>> {
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of req) {
      size += (chunk as Buffer).length;
      if (size > 64 * 1024) throw new HttpError(413, "That request was too large.");
      chunks.push(chunk as Buffer);
    }
    if (size === 0) return {};
    try {
      const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
    } catch {
      // fall through to the 422 below
    }
    throw new HttpError(422, "The server did not accept those values.");
  }

  private requireCode(req: http.IncomingMessage): void {
    if (this.options.code === null) return;
    const given = req.headers["x-access-code"];
    if (typeof given !== "string" || given === "") throw new HttpError(401, "This needs the demo's access code.");
    if (given !== this.options.code) throw new HttpError(403, "That access code is not right.");
  }

  private async inject(target: Target, res: http.ServerResponse, req: http.IncomingMessage): Promise<"handled" | "empty" | null> {
    const injection = this.injections.get(target);
    if (!injection) return null;
    if (injection.mode === "slow") {
      await new Promise((r) => setTimeout(r, injection.ms ?? 1500));
      return null;
    }
    if (injection.mode === "error") {
      this.send(req, res, 500, { detail: "Injected mock error." });
      return "handled";
    }
    return "empty";
  }

  private async handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://mock");
    const route = `${req.method ?? "GET"} ${url.pathname}`;
    if (req.method === "OPTIONS") {
      this.cors(req, res);
      res.writeHead(204).end();
      return;
    }
    try {
      await this.route(req, res, url, route);
    } catch (cause) {
      if (cause instanceof HttpError) {
        const headers: Record<string, string> = cause.retryAfter ? { "Retry-After": String(cause.retryAfter) } : {};
        this.send(req, res, cause.status, { detail: cause.message }, headers);
        return;
      }
      throw cause;
    }
  }

  private async route(req: http.IncomingMessage, res: http.ServerResponse, url: URL, route: string): Promise<void> {
    const send = (status: number, body: unknown, headers?: Record<string, string>): void => this.send(req, res, status, body, headers);

    // --- test controls
    if (route === "POST /__mock/inject") {
      const body = await this.readBody(req);
      const target = body["target"] as Target;
      const mode = body["mode"] as Injection["mode"] | "off";
      if (mode === "off") this.injections.delete(target);
      else this.injections.set(target, { mode, ms: typeof body["ms"] === "number" ? body["ms"] : undefined });
      return send(200, { ok: true });
    }
    if (route === "POST /__mock/pause") {
      this.paused = (await this.readBody(req))["paused"] === true;
      return send(200, { ok: true });
    }
    if (route === "POST /__mock/bench") {
      // Test data for the Bench states: { kind: "scripted" | "real" | "none" }.
      const kind = (await this.readBody(req))["kind"];
      const rows = scenarios().map((sc, i) => ({
        id: sc.id,
        category: sc.category,
        baseline: { attack_succeeded: i % 3 !== 0, task_completed: true },
        full: { attack_succeeded: false, task_completed: i % 7 !== 0 },
      }));
      this.benchResults =
        kind === "scripted"
          ? scriptedRecord()
          : kind === "real"
            ? { provider: "mock-test-data", model: "mock-model (test data, not a result)", date: "2026-11-04", scenarios: rows }
            : null;
      return send(200, { ok: true });
    }
    if (route === "POST /__mock/reset") {
      this.benchResults = null;
      for (const run of this.runs.values()) run.requestStop();
      this.runs.clear();
      this.order = [];
      this.injections.clear();
      this.paused = false;
      this.runsToday = 0;
      return send(200, { ok: true });
    }
    if (route === "POST /__mock/seed") {
      await this.seed();
      return send(200, { ok: true });
    }

    // --- health and demo
    if (route === "GET /healthz") return send(200, { ok: true });
    if (route === "GET /readyz") return send(200, { ready: true, checks: {} });
    if (route === "GET /demo/status") {
      const gate = await this.inject("demo", res, req);
      if (gate === "handled") return;
      const perDay = this.options.runsPerDay ?? 40;
      const status: DemoStatus = {
        access_required: this.options.code !== null,
        public: this.options.code !== null,
        paused: this.paused,
        message: this.paused ? "New runs are paused while the demo is being looked after. Past runs stay readable." : null,
        active_runs: [...this.runs.values()].filter((r) => !r.finished).length,
        max_concurrent: 2,
        runs_today: this.runsToday,
        runs_per_day: perDay,
        runs_left_today: Math.max(0, perDay - this.runsToday),
        runs_per_client_per_hour: 3,
        tokens_today: 0,
        daily_token_cap: 2_000_000,
        run_token_cap: 60_000,
        resets_at: Math.floor(Date.now() / 86_400_000 + 1) * 86_400,
        sandbox_runtime: { requested: "runc", active: "runc", gvisor: false, label: "runc (mock)", reason: null },
        provider_ready: true,
      };
      return send(200, status);
    }
    if (route === "POST /demo/unlock") {
      this.requireCode(req);
      return send(200, { ok: true });
    }

    // --- catalogue
    if (route === "GET /repos") {
      const gate = await this.inject("repos", res, req);
      if (gate === "handled") return;
      return send(200, { repos: gate === "empty" ? [] : ["invoice-calc"] });
    }
    if (route === "GET /bench/scenarios") {
      const gate = await this.inject("gallery", res, req);
      if (gate === "handled") return;
      const all = gate === "empty" ? [] : scenarios();
      return send(200, { total: all.length, scenarios: all, categories: gate === "empty" ? [] : categories() });
    }
    if (route === "GET /bench/results") {
      const gate = await this.inject("bench", res, req);
      if (gate === "handled") return;
      if (gate === "empty" || this.benchResults === null) return send(404, { detail: "No Bench results yet." });
      return send(200, this.benchResults);
    }
    if (route === "GET /policy") {
      const gate = await this.inject("policy", res, req);
      if (gate === "handled") return;
      return send(200, policyDoc);
    }

    // --- runs
    if (route === "POST /runs") {
      this.requireCode(req);
      if (this.paused) throw new HttpError(503, "New runs are paused while the demo is being looked after.");
      const active = [...this.runs.values()].find((r) => !r.finished);
      if (active && [...this.runs.values()].filter((r) => !r.finished).length >= 2) {
        throw new HttpError(409, `A run is already in progress: ${[...this.runs.values()].filter((r) => !r.finished).map((r) => r.meta.id).join(", ")}`);
      }
      if (this.options.runsPerDay !== null && this.runsToday >= this.options.runsPerDay) throw new HttpError(429, "No runs left today.", 3600);
      const body = await this.readBody(req);
      const repo = typeof body["repo"] === "string" ? body["repo"] : undefined;
      const scenario = typeof body["scenario"] === "string" ? body["scenario"] : undefined;
      if ((repo === undefined) === (scenario === undefined)) throw new HttpError(422, "Send exactly one of repo and scenario.");
      if (repo !== undefined && repo !== "invoice-calc") throw new HttpError(404, "No such repo.");
      if (scenario !== undefined && !scenarios().some((s) => s.id === scenario)) throw new HttpError(404, "No such scenario.");
      const fake = typeof body["fake_scenario"] === "string" ? body["fake_scenario"] : undefined;
      if (fake !== undefined && !(SCENARIO_IDS as readonly string[]).includes(fake)) throw new HttpError(422, "Unknown scripted scenario.");
      const task = typeof body["task"] === "string" ? body["task"] : undefined;
      if (task !== undefined && (task.trim() === "" || task.length > 2000)) throw new HttpError(422, "The task must be 1 to 2000 characters.");
      const run = this.startRun(
        { repo, scenario, task, guard: body["guard"] !== false, approve_plan: body["approve_plan"] === true },
        fake as ScenarioId | undefined,
      );
      return send(202, { id: run.meta.id });
    }
    if (route === "GET /runs") {
      const gate = await this.inject("runs", res, req);
      if (gate === "handled") return;
      const limit = Math.min(100, Math.max(1, Number(url.searchParams.get("limit") ?? 20) || 20));
      const offset = Math.max(0, Number(url.searchParams.get("offset") ?? 0) || 0);
      const ids = gate === "empty" ? [] : this.order;
      const rows = ids.slice(offset, offset + limit).map((id) => runRow(this.runs.get(id) as MockRun));
      return send(200, { runs: rows, total: ids.length, limit, offset });
    }

    const match = /^(GET|POST) \/runs\/([A-Za-z0-9_-]{1,64})(?:\/([a-z/]+))?$/.exec(route);
    if (match) {
      const [, method, id, sub] = match as unknown as [string, string, string, string | undefined];
      const run = this.runs.get(id);
      if (!run) throw new HttpError(404, "No such run.");
      return this.runRoute(req, res, run, method, sub ?? "", url);
    }
    send(404, { detail: "Not found." });
  }

  private async runRoute(req: http.IncomingMessage, res: http.ServerResponse, run: MockRun, method: string, sub: string, url: URL): Promise<void> {
    const send = (status: number, body: unknown, headers?: Record<string, string>): void => this.send(req, res, status, body, headers);
    const key = `${method} ${sub}`;
    switch (key) {
      case "GET ": {
        const gate = await this.inject("run", res, req);
        if (gate === "handled") return;
        // The single-run view also carries the open question, the final event and the clock.
        return send(200, runDetail(run));
      }
      case "GET events":
        return this.stream(req, res, run);
      case "POST stop": {
        this.requireCode(req);
        run.requestStop();
        return send(200, { id: run.meta.id, status: run.finished ? run.status : "stopping" });
      }
      case "POST approve": {
        this.requireCode(req);
        const body = await this.readBody(req);
        const approvalId = body["approval_id"];
        const choiceId = body["choice"];
        if (typeof approvalId !== "string" || typeof choiceId !== "string") throw new HttpError(422, "Send approval_id and choice.");
        const text = typeof body["answer"] === "string" ? body["answer"].slice(0, 1000) : null;
        if (run.pendingApprovalId !== approvalId) throw new HttpError(409, "Nothing is waiting for that answer.");
        const pending = run.events.findLast((e) => e.kind === "approval_needed");
        const choice = pending && pending.kind === "approval_needed" ? pending.approval.choices.find((c) => c.id === choiceId) : undefined;
        if (!choice) throw new HttpError(422, "That choice is not offered.");
        if (pending && pending.kind === "approval_needed" && pending.approval.free_text?.required && choice.approves && (text === null || text.trim() === "")) {
          throw new HttpError(422, "An answer is needed.");
        }
        run.answer(approvalId, choiceId, text);
        return send(200, { id: run.meta.id, approval_id: approvalId, choice: choiceId });
      }
      case "GET checkpoints": {
        const rows = run.events.filter((e) => e.kind === "checkpoint");
        return send(200, { checkpoints: rows });
      }
      case "POST rewind": {
        this.requireCode(req);
        if (run.finished) throw new HttpError(409, "The run is not acting.");
        const body = await this.readBody(req);
        const checkpoint = body["checkpoint"];
        if (typeof checkpoint !== "number") throw new HttpError(422, "Send a checkpoint number.");
        const known = checkpoint === 0 || run.events.some((e) => e.kind === "checkpoint" && e.checkpoint === checkpoint);
        if (!known) throw new HttpError(422, "Unknown checkpoint.");
        run.emit({ kind: "checkpoint", phase: "act", checkpoint, state: "restored", files: [], step: null, restored_by: "human", error: null });
        return send(202, { state: "requested" });
      }
      case "GET patch": {
        const gate = await this.inject("patch", res, req);
        if (gate === "handled") return;
        if (!run.finished || run.outputs.patch === null) throw new HttpError(409, "The run has not delivered a patch.");
        if (run.outputs.blocked && !run.outputs.released) throw new HttpError(423, "The patch is withheld.");
        return send(200, run.outputs.patch, { "Content-Disposition": 'attachment; filename="sandcage.patch"' });
      }
      case "POST release": {
        this.requireCode(req);
        if (!run.outputs.blocked || run.outputs.released) throw new HttpError(409, "The patch is not withheld.");
        run.outputs = { ...run.outputs, released: true };
        return send(200, { released: true });
      }
      case "GET report": {
        if (!run.finished) throw new HttpError(409, "The run has not finished.");
        const format = url.searchParams.get("format") ?? "json";
        if (format === "md") return send(200, reportMarkdown(run.meta.id, run.events));
        if (format !== "json") throw new HttpError(422, "Format must be md or json.");
        return send(200, { run_id: run.meta.id, mock: true, events: run.events.length });
      }
      case "GET summary":
        return send(200, "Mock summary.\n");
      case "POST learn/accept": {
        this.requireCode(req);
        const body = await this.readBody(req);
        const learn = run.events.find((e): e is Extract<Event, { kind: "learn" }> => e.kind === "learn");
        const suggestion = learn?.suggestions.find((s) => s.id === body["suggestion"]);
        if (!suggestion) throw new HttpError(404, "No such suggestion.");
        return send(200, { rule_id: suggestion.rule_id, file: suggestion.target });
      }
      default:
        throw new HttpError(404, "Not found.");
    }
  }

  private stream(req: http.IncomingMessage, res: http.ServerResponse, run: MockRun): void {
    this.cors(req, res);
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store, no-transform", Connection: "keep-alive", "X-Accel-Buffering": "no" });
    const lastHeader = req.headers["last-event-id"];
    const last = typeof lastHeader === "string" && /^\d+$/.test(lastHeader) ? Number(lastHeader) : 0;
    const write = (e: Event): void => {
      res.write(`id: ${e.id}\nevent: ${e.kind}\ndata: ${JSON.stringify(e)}\n\n`);
      if (e.kind === "done") res.end();
    };
    for (const e of run.events) if (e.id > last) write(e);
    if (res.writableEnded) return;
    const unsubscribe = run.subscribe((e) => {
      if (e.id > last) write(e);
    });
    const beat = setInterval(() => res.write(": ping\n\n"), this.options.heartbeatMs);
    const cleanup = (): void => {
      clearInterval(beat);
      unsubscribe();
    };
    res.on("close", cleanup);
  }
}

class HttpError extends Error {
  readonly status: number;
  readonly retryAfter: number | null;
  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}
