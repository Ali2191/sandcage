/**
 * Everything a run page shows, derived from the ordered event log with pure functions.
 * The log is the only state: a reload or a Last-Event-ID resume just rebuilds the log
 * and calls these again. Nothing here reads a clock; callers pass `now` where needed.
 */
import type {
  Approval,
  ApprovalNeededEvent,
  BreakerEvent,
  Budget,
  CheckpointEvent,
  DeliverEvent,
  DoneEvent,
  Event,
  LearnEvent,
  Plan,
  PlanEvent,
  RedteamEvent,
  ReportEvent,
  ScoutEvent,
  StepEvent,
  Trifecta,
  UnderstandEvent,
  VerifyEvent,
} from "@contract";
import { isKnown, type LogEvent } from "./events";

type Kind = Event["kind"];
type EventOf<K extends Kind> = Extract<Event, { kind: K }>;

export function ofKind<K extends Kind>(events: readonly Event[], kind: K): EventOf<K>[] {
  return events.filter((e): e is EventOf<K> => e.kind === kind);
}

export function lastOfKind<K extends Kind>(events: readonly Event[], kind: K): EventOf<K> | undefined {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i];
    if (e !== undefined && e.kind === kind) return e as EventOf<K>;
  }
  return undefined;
}

export function known(log: readonly LogEvent[]): Event[] {
  return log.filter(isKnown);
}

export function steps(events: readonly Event[]): StepEvent[] {
  return ofKind(events, "step");
}

export function doneEvent(events: readonly Event[]): DoneEvent | undefined {
  return lastOfKind(events, "done");
}

export function isFinished(events: readonly Event[]): boolean {
  return doneEvent(events) !== undefined;
}

// ---------------------------------------------------------------------------
// Pending approval

export interface PendingApproval {
  /** The id of the approval_needed event that raised it. */
  eventId: number;
  approval: Approval;
  /** When the server stops waiting (Unix ms): the event time plus the timeout. */
  deadlineMs: number;
}

/**
 * The pending approval is the last `approval_needed` without a matching
 * `approval_resolved`. A finished run waits for nothing.
 */
export function pendingApproval(events: readonly Event[]): PendingApproval | null {
  if (isFinished(events)) return null;
  const resolved = new Set<string>();
  for (const e of events) if (e.kind === "approval_resolved") resolved.add(e.approval_id);
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i];
    if (e === undefined || e.kind !== "approval_needed") continue;
    if (resolved.has(e.approval.id)) continue;
    return toPending(e);
  }
  return null;
}

function toPending(e: ApprovalNeededEvent): PendingApproval {
  return {
    eventId: e.id,
    approval: e.approval,
    deadlineMs: Math.round((e.ts + e.approval.timeout_seconds) * 1000),
  };
}

/**
 * Seconds left to answer, never more than the timeout (a browser clock that runs behind
 * the server's must not show a longer wait than the server grants) and never below zero.
 */
export function secondsLeft(deadlineMs: number, nowMs: number, timeoutSeconds: number): number {
  const left = Math.ceil((deadlineMs - nowMs) / 1000);
  return Math.max(0, Math.min(timeoutSeconds, left));
}

/** "m:ss" */
export function clock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The countdown is spoken only at these thresholds (seconds left). */
export const COUNTDOWN_ANNOUNCE_AT: readonly number[] = [60, 30, 10, 5, 0];

/**
 * The text a screen reader should hear for the countdown, or null when this second is
 * not a threshold. Callers keep the last spoken value so each threshold fires once.
 */
export function countdownAnnouncement(left: number): string | null {
  if (!COUNTDOWN_ANNOUNCE_AT.includes(left)) return null;
  if (left === 0) return "Time is up. This counts as no.";
  return `${left} seconds left to answer`;
}

// ---------------------------------------------------------------------------
// Stage pills

export type StageKey = "plan" | "lock" | "act" | "verify" | "redteam" | "deliver";
export type StageState = "done" | "current" | "waiting" | "failed" | "upcoming";

export interface Stage {
  key: StageKey;
  label: string;
  state: StageState;
  /** Extra text for the pill, e.g. "step 6" on Act. */
  detail: string | null;
}

const STAGES: readonly { key: StageKey; label: string }[] = [
  { key: "plan", label: "Plan" },
  { key: "lock", label: "Lock" },
  { key: "act", label: "Act" },
  { key: "verify", label: "Verify" },
  { key: "redteam", label: "Red-team" },
  { key: "deliver", label: "Deliver" },
];

const TAIL: ReadonlySet<StageKey> = new Set<StageKey>(["redteam", "deliver"]);

function stageIndex(key: StageKey): number {
  return STAGES.findIndex((s) => s.key === key);
}

/** The furthest stage the log has reached, or -1 before the plan starts. */
function furthestStage(events: readonly Event[]): number {
  let furthest = -1;
  const reach = (key: StageKey): void => {
    furthest = Math.max(furthest, stageIndex(key));
  };
  for (const e of events) {
    switch (e.kind) {
      case "plan":
        reach(e.state === "locked" || e.state === "amended" ? "lock" : "plan");
        break;
      case "step":
        reach("act");
        break;
      case "checkpoint":
      case "breaker":
      case "guard":
        reach("act");
        break;
      case "verify":
        reach("verify");
        break;
      case "redteam":
        reach("redteam");
        break;
      case "deliver":
      case "report":
      case "learn":
        reach("deliver");
        break;
      case "approval_needed":
        if (e.approval.kind === "plan") reach("plan");
        if (e.approval.kind === "redteam") reach("redteam");
        break;
      default:
        break;
    }
  }
  return furthest;
}

export function stages(events: readonly Event[]): Stage[] {
  const furthest = furthestStage(events);
  const done = doneEvent(events);
  const waiting = pendingApproval(events) !== null;
  const lastStep = steps(events).at(-1);
  const failedStatus = done !== undefined && done.status !== "finished" && done.status !== "stopped";

  return STAGES.map(({ key, label }, index): Stage => {
    let state: StageState;
    if (index > furthest) state = "upcoming";
    else if (done !== undefined) {
      const isLast = index === furthest;
      state = failedStatus && isLast && !TAIL.has(key) ? "failed" : "done";
    } else if (index < furthest) state = "done";
    else state = waiting ? "waiting" : "current";
    const detail = key === "act" && state !== "done" && state !== "upcoming" && lastStep ? `step ${lastStep.step}` : null;
    return { key, label, state, detail };
  });
}

// ---------------------------------------------------------------------------
// Plan, understanding, scout

export function latestPlanEvent(events: readonly Event[]): PlanEvent | undefined {
  const plans = ofKind(events, "plan").filter((p) => p.plan !== null);
  return plans.at(-1);
}

export function latestPlan(events: readonly Event[]): Plan | null {
  return latestPlanEvent(events)?.plan ?? doneEvent(events)?.plan ?? null;
}

export function understanding(events: readonly Event[]): UnderstandEvent | undefined {
  const all = ofKind(events, "understand").filter((u) => u.valid);
  return all.at(-1);
}

export function scoutEvent(events: readonly Event[]): ScoutEvent | undefined {
  return lastOfKind(events, "scout");
}

/** Paths the scout flagged as carrying instructions aimed at an AI. */
export function flaggedPaths(events: readonly Event[]): string[] {
  const fromScout = scoutEvent(events)?.flags.map((f) => f.path) ?? [];
  const fromDone = doneEvent(events)?.scout_flags ?? [];
  return [...new Set([...fromScout, ...fromDone])];
}

/** A run that needed a human to approve the plan, and got it. */
export function planApprovedByHuman(events: readonly Event[]): boolean {
  return ofKind(events, "plan").some((p) => p.approved_by_human);
}

// ---------------------------------------------------------------------------
// Budget

export function latestBudget(events: readonly Event[]): Budget | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const b = events[i]?.budget;
    if (b) return b;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Steps: blocked, counts, containment moment

export function blockedSteps(events: readonly Event[]): StepEvent[] {
  return steps(events).filter((s) => s.blocked);
}

/** A real gate denial: the agent tried something the rules forbid (and it did not run). */
export function deniedSteps(events: readonly Event[]): StepEvent[] {
  return steps(events).filter((s) => s.blocked && !s.observed && s.decision === "deny");
}

/** An ask a human did not approve (refused or timed out). It did not run, but it is not an attack. */
export function declinedSteps(events: readonly Event[]): StepEvent[] {
  return steps(events).filter((s) => s.blocked && !s.observed && s.decision === "ask");
}

export interface GateCounts {
  checked: number;
  /** Gate denials only, as the report counts them; refused asks are `declined`. */
  blocked: number;
  declined: number;
  allowed: number;
  denied: number;
  asked: number;
}

export function gateCounts(events: readonly Event[]): GateCounts {
  const all = steps(events);
  const count = (d: StepEvent["decision"]): number => all.filter((s) => s.decision === d).length;
  return {
    checked: all.length,
    blocked: deniedSteps(events).length,
    declined: declinedSteps(events).length,
    allowed: count("allow"),
    denied: count("deny"),
    asked: count("ask"),
  };
}

/**
 * The containment moment: the first step the gate denied, with Sandcage's own sentence
 * for it. The top bar shows this the moment it happens. A question the human refused is not
 * a containment moment: nothing was attacked.
 */
export function containmentMoment(events: readonly Event[]): { step: StepEvent; sentence: string } | null {
  const first = deniedSteps(events)[0];
  if (!first) return null;
  return { step: first, sentence: blockedLine(first) };
}

/**
 * Sandcage's sentence for a blocked step. The backend writes it from the action and the
 * rule; the agent's own words are never used. A blocked step without a sentence (a
 * contract gap, see CONTRACT-REQUESTS.md) falls back to the rule's reason.
 */
export function blockedLine(step: StepEvent): string {
  if (step.blocked_sentence !== null && step.blocked_sentence.trim() !== "") return step.blocked_sentence;
  return step.reason.trim() !== "" ? step.reason : `Blocked by ${step.rule_id}`;
}

// ---------------------------------------------------------------------------
// Breaker text

export interface BreakerText {
  headline: string;
  detail: string;
}

function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1);
}

function withoutTriedTo(sentence: string): string {
  return lowerFirst(sentence.replace(/^Tried to /, ""));
}

function times(n: number): string {
  return n === 1 ? "once" : `${n} times`;
}

/**
 * Plain wording for a tripped breaker, built from the blocked steps since the last
 * resume. Says what happened once, without repeating itself. `source` is the file the
 * breaker traced the attack to, when known.
 */
export function breakerText(blocked: readonly StepEvent[], source: string | null): BreakerText {
  const headline =
    source !== null
      ? `Something in ${source} is steering the agent, so Sandcage paused it.`
      : "The agent keeps trying things it isn't allowed to, so Sandcage paused it.";
  const things: string[] = [];
  for (const s of blocked) {
    const thing = withoutTriedTo(blockedLine(s));
    if (!things.includes(thing)) things.push(thing);
  }
  const lead = source !== null ? `Blocked ${times(blocked.length)} after it read ${source}.` : `Blocked ${times(blocked.length)}.`;
  const [first, second, ...rest] = things;
  let tried = "";
  if (first !== undefined) {
    tried = ` It tried to ${first}`;
    if (second !== undefined) tried += `, then ${second}`;
    if (rest.length > 0) tried += `, and ${rest.length} more`;
    tried += ".";
  }
  return { headline, detail: lead + tried };
}

/** Blocked steps since the last resume of the breaker (what the next trip would list). */
export function blockedSinceLastResume(events: readonly Event[]): StepEvent[] {
  let from = 0;
  events.forEach((e, i) => {
    if (e.kind === "breaker" && e.state === "resumed") from = i + 1;
  });
  return steps(events.slice(from)).filter((s) => s.blocked);
}

export function breakerEvents(events: readonly Event[]): BreakerEvent[] {
  return ofKind(events, "breaker");
}

// ---------------------------------------------------------------------------
// Blast radius: what the agent reached for, and what it changed

export type ReachKind = "file" | "folder" | "command" | "host";

export interface Reach {
  target: string;
  kind: ReachKind;
  /** The gate stopped it every time it was tried. */
  blocked: boolean;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

const URL_IN_TEXT = /https?:\/\/([A-Za-z0-9.-]+)(?::\d+)?(?:[/?#]\S*)?/g;

/** Hosts named by URLs inside a piece of text, in order, once each. */
export function hostsIn(text: string): string[] {
  const hosts: string[] = [];
  for (const match of text.matchAll(URL_IN_TEXT)) {
    const host = (match[1] ?? "").replace(/\.+$/, "").toLowerCase();
    if (host !== "" && !hosts.includes(host)) hosts.push(host);
  }
  return hosts;
}

function hostOfUrl(url: string): string | null {
  const [host] = hostsIn(url);
  return host ?? null;
}

interface RawReach {
  target: string;
  kind: ReachKind;
  touched: boolean;
}

function reachesOf(step: StepEvent): RawReach[] {
  const touched = step.executed;
  const out: RawReach[] = [];
  const path = str(step.args["path"]);
  switch (step.tool) {
    case "read_file":
    case "write_file":
      if (path) out.push({ target: path, kind: "file", touched });
      break;
    case "list_dir":
      out.push({ target: path === null || path === "." ? "./" : path, kind: "folder", touched });
      break;
    case "run_command": {
      const command = str(step.args["command"]);
      if (command) {
        out.push({ target: command, kind: "command", touched });
        for (const host of hostsIn(command)) out.push({ target: host, kind: "host", touched });
      }
      break;
    }
    case "http_get": {
      const url = str(step.args["url"]);
      const host = url ? hostOfUrl(url) : null;
      if (host) out.push({ target: host, kind: "host", touched });
      break;
    }
    default:
      break; // finish, amend_plan and rollback reach for nothing
  }
  return out;
}

/** Every target the agent reached for. One that was blocked every time is `blocked`. */
export function triedTargets(events: readonly Event[]): Reach[] {
  const order: string[] = [];
  const seen = new Map<string, { kind: ReachKind; touched: boolean }>();
  for (const step of steps(events)) {
    for (const r of reachesOf(step)) {
      const key = `${r.kind}:${r.target}`;
      const prior = seen.get(key);
      if (!prior) {
        order.push(key);
        seen.set(key, { kind: r.kind, touched: r.touched });
      } else if (r.touched) prior.touched = true;
    }
  }
  return order.map((key) => {
    const entry = seen.get(key) as { kind: ReachKind; touched: boolean };
    return { target: key.slice(key.indexOf(":") + 1), kind: entry.kind, blocked: !entry.touched };
  });
}

/**
 * Files the run changed, never commands: the delivered patch's files, else the files
 * the run reported changing, else files an executed write touched.
 */
export function changedFiles(events: readonly Event[]): string[] {
  const deliver = lastOfKind(events, "deliver");
  if (deliver && deliver.files.length > 0) return [...deliver.files];
  const done = doneEvent(events);
  if (done && done.changed_files.length > 0) return [...done.changed_files];
  const written: string[] = [];
  for (const s of steps(events)) {
    const path = str(s.args["path"]);
    if (s.tool === "write_file" && s.executed && path && !written.includes(path)) written.push(path);
  }
  return written;
}

// ---------------------------------------------------------------------------
// Verify, red-team, deliver, report, learn, checkpoints

export function latestVerify(events: readonly Event[]): VerifyEvent | undefined {
  return lastOfKind(events, "verify");
}

export function redteam(events: readonly Event[]): RedteamEvent | undefined {
  return lastOfKind(events, "redteam");
}

export function deliver(events: readonly Event[]): DeliverEvent | undefined {
  return lastOfKind(events, "deliver");
}

export function reportEvent(events: readonly Event[]): ReportEvent | undefined {
  return lastOfKind(events, "report");
}

export function learnEvent(events: readonly Event[]): LearnEvent | undefined {
  return lastOfKind(events, "learn");
}

export function trifecta(events: readonly Event[]): Trifecta | null {
  return reportEvent(events)?.trifecta ?? doneEvent(events)?.trifecta ?? null;
}

export type LegState = "present" | "blocked" | "absent";

export interface TrifectaLegs {
  privateData: LegState;
  untrustedContent: LegState;
  wayOut: LegState;
  allAtOnce: boolean;
}

/**
 * The three legs as the card shows them. A leg is "present" when the run had it; the way
 * out reads "blocked" when the gate stopped a network attempt (the report counted one).
 */
export function trifectaLegs(t: Trifecta, report: ReportEvent | undefined): TrifectaLegs {
  const network = report?.capabilities.find((c) => c.capability === "network");
  const wayOut: LegState = t.way_out ? "present" : network && network.blocked > 0 ? "blocked" : "absent";
  return {
    privateData: t.private_data ? "present" : "absent",
    untrustedContent: t.untrusted_content ? "present" : "absent",
    wayOut,
    allAtOnce: t.all_at_once,
  };
}

export interface CheckpointRow {
  id: number;
  /** "the original repo" for 0, else "after step n" (null when the step is unknown). */
  step: number | null;
  restoredBy: "agent" | "human" | null;
}

export interface Checkpoints {
  rows: CheckpointRow[];
  /** The last restore, for "Restored checkpoint n by you." */
  lastRestore: { id: number; by: "agent" | "human" } | null;
}

export function checkpoints(events: readonly Event[]): Checkpoints {
  const rows = new Map<number, CheckpointRow>([[0, { id: 0, step: null, restoredBy: null }]]);
  let lastRestore: Checkpoints["lastRestore"] = null;
  for (const e of ofKind(events, "checkpoint")) {
    applyCheckpoint(rows, e);
    if (e.state === "restored" && e.restored_by) lastRestore = { id: e.checkpoint, by: e.restored_by };
  }
  return { rows: [...rows.values()].sort((a, b) => a.id - b.id), lastRestore };
}

function applyCheckpoint(rows: Map<number, CheckpointRow>, e: CheckpointEvent): void {
  if (e.state === "failed") return;
  const prior = rows.get(e.checkpoint);
  rows.set(e.checkpoint, {
    id: e.checkpoint,
    step: e.step ?? prior?.step ?? null,
    restoredBy: e.state === "restored" ? e.restored_by : (prior?.restoredBy ?? null),
  });
}

/** A human may rewind only while the agent is acting and nothing waits on them. */
export function canRewind(events: readonly Event[]): boolean {
  if (isFinished(events)) return false;
  const current = stages(events).find((s) => s.state === "current");
  return current?.key === "act";
}

// ---------------------------------------------------------------------------
// Outcome: the pill and the headline case

export type Outcome =
  | { kind: "running" }
  | { kind: "delivered" }
  | { kind: "released" }
  | { kind: "withheld" }
  | { kind: "stopped" }
  | { kind: "ended"; status: string };

/**
 * How the run ended. A patch the red-team withheld is "withheld" until a human releases
 * it; it is never "delivered" and the report never says the task got done.
 * `releasedAfterDone` is true when the run info says a human released it after the run
 * ended (the log is closed by `done`, so a later release cannot appear in it).
 */
export function outcome(events: readonly Event[], releasedAfterDone = false): Outcome {
  const done = doneEvent(events);
  if (!done) return { kind: "running" };
  if (done.status === "stopped") return { kind: "stopped" };
  if (done.status !== "finished") return { kind: "ended", status: done.status };
  const d = deliver(events);
  const blocked = done.delivery_blocked || (d?.blocked ?? false);
  const released = releasedAfterDone || done.released_by_human || (d?.released_by_human ?? false);
  if (blocked && !released) return { kind: "withheld" };
  if (released) return { kind: "released" };
  return { kind: "delivered" };
}

/** Whether a patch exists to show a card for (never for a stopped or undelivered run). */
export function hasPatchCard(events: readonly Event[]): boolean {
  const o = outcome(events);
  if (o.kind === "stopped") return false;
  const done = doneEvent(events);
  return deliver(events) !== undefined || (done?.delivered ?? false);
}

/** Sandcage's runtime label for the cage, only when the run reported one. */
export function sandboxRuntime(events: readonly Event[]): string | null {
  return doneEvent(events)?.sandbox_runtime ?? null;
}

/**
 * Tokens as a measured number, or null. Estimated counts are never shown as measured.
 */
export function measuredTokens(budget: Budget | null): number | null {
  return budget !== null && budget.tokens_reported ? budget.tokens : null;
}

/** Network attempts the run recorded: hosts of blocked or executed network targets. */
export function networkAttempts(events: readonly Event[]): { hosts: string[]; bytesSent: 0 | null } {
  const hosts = triedTargets(events).filter((t) => t.kind === "host");
  const anyTouched = hosts.some((h) => !h.blocked);
  return { hosts: hosts.map((h) => h.target), bytesSent: hosts.length > 0 && !anyTouched ? 0 : null };
}

/** The first six characters of a run id. */
export function shortId(runId: string): string {
  return runId.slice(0, 6);
}
