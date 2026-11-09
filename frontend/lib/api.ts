/**
 * The only module that talks to the Sandcage API. Pointing the panel at the real API
 * means changing NEXT_PUBLIC_API_URL (the mock API in ../mock serves the same shapes).
 *
 * Event payloads are typed by the contract (../../contract). The non-event payloads below
 * (run rows, demo status, catalogue, policy, bench) have no contract type yet: they are
 * adapter types written from Specs/API.md and listed in CONTRACT-REQUESTS.md.
 */
import type { Approval, DoneEvent } from "../../contract/events";
import { accessCode } from "./access";

export const API_BASE: string = (process.env["NEXT_PUBLIC_API_URL"] ?? "http://127.0.0.1:8001").replace(/\/+$/, "");

const TIMEOUT_MS = 10_000;

export class ApiError extends Error {
  readonly status: number | null;
  /** Seconds the server asked us to wait, for 429 and 503. */
  readonly retryAfter: number | null;
  constructor(message: string, status: number | null, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }
  /** The demo's access code is missing or wrong. */
  get needsCode(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

// --- adapter types -----------------------------------------------------------

export interface DecisionCountsRow {
  allowed: number;
  denied: number;
  asked: number;
  injection_tagged: number;
}

export interface RunRow {
  id: string;
  repo: string;
  task: string;
  guard: boolean;
  status: string;
  created: number;
  finished: number | null;
  provider: string | null;
  model: string | null;
  steps: number;
  counts: DecisionCountsRow | null;
  tokens: number | null;
  /** True only when the provider reported the token count. */
  tokens_reported: boolean;
  /** The run's final outcome pill ("Delivered", "Withheld", ...); null while it runs. */
  outcome: string | null;
  delivered: boolean;
  /** Set once the run ended: the sandbox runtime's label, as `done` reported it. */
  sandbox_runtime: string | null;
  /** A human released a withheld patch, during the run or after it (the log is closed by `done`). */
  released_by_human: boolean;
  delivery_blocked: boolean;
}

/** `GET /runs/{id}`: the row plus the open question, the final event and the server's clock. */
export interface RunDetail extends RunRow {
  pending: Approval | null;
  result: DoneEvent | null;
  /** Unix seconds on the server, to correct countdowns for clock skew. */
  server_time: number;
}

export interface RunList {
  runs: RunRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface SandboxRuntimeStatus {
  requested: string;
  active: string | null;
  gvisor: boolean;
  label: string;
  reason: string | null;
}

export interface DemoStatus {
  access_required: boolean;
  public: boolean;
  paused: boolean;
  message: string | null;
  active_runs: number;
  max_concurrent: number;
  runs_today: number;
  runs_per_day: number;
  runs_left_today: number;
  runs_per_client_per_hour: number;
  tokens_today: number;
  daily_token_cap: number;
  run_token_cap: number;
  /** Unix seconds of the next UTC midnight. */
  resets_at: number;
  sandbox_runtime: SandboxRuntimeStatus | null;
  provider_ready: boolean;
}

export interface BenchScenario {
  id: string;
  category: string;
  category_label: string;
  title: string;
  blurb: string;
  task: string;
  /** Where the attack arrives and what it is after (the backend's own ids). */
  channel?: string;
  goal?: string;
  /** An ordinary task with nothing to contain: Sandcage must not stop it. */
  benign?: boolean;
}

export interface BenchCategory {
  key: string;
  label: string;
  count: number;
}

export interface BenchCatalogue {
  total: number;
  scenarios: BenchScenario[];
  categories: BenchCategory[];
}

export interface PolicyRule {
  id: string;
  decision: "allow" | "ask" | "deny";
  risk: string | null;
  reason: string;
  tools: string[];
}

export interface PolicyDoc {
  rules: PolicyRule[];
  secret_patterns: string[];
  counts: { deny: number; ask: number; allow: number };
}

/** One scenario in one mode. Both judgements are null when the run crashed (`error` says why). */
export interface BenchModeResult {
  attack_succeeded: boolean | null;
  task_completed: boolean | null;
  error?: string | null;
  note?: string | null;
}

export interface BenchResults {
  provider: string;
  model: string | null;
  date: string | null;
  /** "scoring check" for a scripted model, "results" for a real one (as the backend wrote it). */
  label?: string;
  scripted?: boolean;
  notes?: string[];
  /** Runs that crashed (each is a failed result, never a missing one). */
  errors?: number;
  /** Scripted only: every run came out as designed. */
  as_expected?: boolean;
  scenarios: {
    id: string;
    category: string;
    /** A benign scenario has nothing to contain; it is left out of the attack rate. */
    benign?: boolean;
    baseline: BenchModeResult | null;
    full: BenchModeResult | null;
  }[];
}

export interface PatchResponse {
  text: string;
}

export type ReportFormat = "md" | "json";

export interface LearnAccepted {
  rule_id: string;
  file: string;
}

export interface ApproveBody {
  approval_id: string;
  choice: string;
  answer?: string;
}

export interface StartBody {
  repo?: string;
  scenario?: string;
  /** Scripted scenario id: the run follows its script and no model is called, on any server. */
  fake_scenario?: string;
  task?: string;
  guard: boolean;
  approve_plan: boolean;
}

// --- plumbing ------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function describeStatus(status: number): string {
  switch (status) {
    case 401:
      return "This needs the demo's access code.";
    case 403:
      return "That access code is not right.";
    case 404:
      return "That doesn't exist on the server.";
    case 409:
      return "That isn't possible in the run's current state.";
    case 413:
      return "That request was too large.";
    case 422:
      return "The server did not accept those values.";
    case 423:
      return "That is withheld until a human releases it.";
    case 429:
      return "Too many requests. Try again in a little while.";
    case 503:
      return "The server isn't taking that right now.";
    default:
      return status >= 500 ? "The server hit a problem." : "The request failed.";
  }
}

/** A short message the server may send for people (paused, rate limited, busy). */
function serverMessage(body: unknown): string | null {
  if (!isRecord(body)) return null;
  const detail = body["detail"];
  if (typeof detail === "string" && detail.trim() !== "" && detail.length <= 300) return detail;
  return null;
}

async function request(path: string, init: RequestInit = {}, withCode = false): Promise<Response> {
  const headers = new Headers(init.headers);
  if (withCode) {
    const code = accessCode();
    if (code) headers.set("X-Access-Code", code);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    return await fetch(`${API_BASE}${path}`, { ...init, headers, signal: controller.signal, cache: "no-store" });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw new ApiError("The Sandcage API took too long to answer. Check that the backend is running.", null);
    }
    throw new ApiError(
      "Can't reach the Sandcage API. Check that the backend is running and that this page's address is an allowed origin.",
      null,
    );
  } finally {
    clearTimeout(timer);
  }
}

async function fail(response: Response, useServerMessage: boolean): Promise<never> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null; // an error page that is not JSON: the status alone tells the story
  }
  const retry = Number(response.headers.get("retry-after"));
  // The server's own words are for the cases written for people (busy, rate limited, paused);
  // a 400, 401, 403 or 413 never shows raw API text.
  const friendly = response.status === 409 || response.status === 429 || response.status === 503;
  const message = (useServerMessage && friendly ? serverMessage(body) : null) ?? describeStatus(response.status);
  throw new ApiError(message, response.status, Number.isFinite(retry) && retry > 0 ? retry : null);
}

async function json<T>(path: string, init: RequestInit = {}, opts: { code?: boolean; serverMessage?: boolean } = {}): Promise<T> {
  const response = await request(path, init, opts.code ?? false);
  if (!response.ok) return fail(response, opts.serverMessage ?? false);
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("The server sent an answer the panel could not read.", response.status);
  }
}

function post(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

// --- endpoints -----------------------------------------------------------------

export const listRepos = async (): Promise<string[]> => {
  const data = await json<{ repos?: unknown }>("/repos");
  const repos = data.repos;
  if (!Array.isArray(repos) || !repos.every((x) => typeof x === "string")) {
    throw new ApiError("The server sent a repository list the panel could not read.", 200);
  }
  return repos;
};

export const getDemoStatus = (): Promise<DemoStatus> => json<DemoStatus>("/demo/status");

export const unlock = (): Promise<{ ok: boolean }> =>
  json("/demo/unlock", { method: "POST" }, { code: true, serverMessage: true });

export const startRun = async (body: StartBody): Promise<{ id: string }> => {
  const out = await json<{ id?: unknown; run_id?: unknown }>("/runs", post(body), { code: true, serverMessage: true });
  const id = typeof out.id === "string" ? out.id : typeof out.run_id === "string" ? out.run_id : null;
  if (id === null) throw new ApiError("The server did not return a run id.", 202);
  return { id };
};

export const listRuns = (limit: number, offset: number): Promise<RunList> =>
  json<RunList>(`/runs?limit=${limit}&offset=${offset}`);

export const getRun = (id: string): Promise<RunDetail> => json<RunDetail>(`/runs/${encodeURIComponent(id)}`);

export const stopRun = (id: string): Promise<{ status: string }> =>
  json(`/runs/${encodeURIComponent(id)}/stop`, { method: "POST" }, { code: true, serverMessage: true });

export const approve = (id: string, body: ApproveBody): Promise<unknown> =>
  json(`/runs/${encodeURIComponent(id)}/approve`, post(body), { code: true });

export const rewind = (id: string, checkpoint: number): Promise<unknown> =>
  json(`/runs/${encodeURIComponent(id)}/rewind`, post({ checkpoint }), { code: true });

export const getPatch = async (id: string): Promise<string> => {
  const response = await request(`/runs/${encodeURIComponent(id)}/patch`);
  if (!response.ok) return fail(response, false);
  return response.text();
};

export const releasePatch = (id: string): Promise<unknown> =>
  json(`/runs/${encodeURIComponent(id)}/release`, { method: "POST" }, { code: true });

export const getReport = async (id: string, format: ReportFormat): Promise<string> => {
  const response = await request(`/runs/${encodeURIComponent(id)}/report?format=${format}`);
  if (!response.ok) return fail(response, false);
  if (format === "json") {
    const parsed: unknown = await response.json();
    return JSON.stringify(parsed, null, 2);
  }
  return response.text();
};

export const acceptSuggestion = (id: string, suggestion: string): Promise<LearnAccepted> =>
  json(`/runs/${encodeURIComponent(id)}/learn/accept`, post({ suggestion }), { code: true });

export const getCatalogue = (): Promise<BenchCatalogue> => json<BenchCatalogue>("/bench/scenarios");

export const getPolicy = (): Promise<PolicyDoc> => json<PolicyDoc>("/policy");

/** Bench results, or null until a Bench run has written any (the API answers 404). */
export const getBenchResults = async (): Promise<BenchResults | null> => {
  const response = await request("/bench/results");
  if (response.status === 404) return null;
  if (!response.ok) return fail(response, false);
  return (await response.json()) as BenchResults;
};

/** A Bench number is a result only when a real model produced it. */
export function isRealBench(results: BenchResults | null): results is BenchResults & { model: string } {
  return results !== null && results.provider !== "fake" && results.provider !== "scripted" && typeof results.model === "string" && results.model !== "";
}

export function eventsUrl(id: string): string {
  return `${API_BASE}/runs/${encodeURIComponent(id)}/events`;
}
