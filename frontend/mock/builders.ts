/**
 * Builders for contract events, used by the mock API's scenario scripts. Everything is
 * typed by the contract, so a contract change breaks the mock at compile time. The
 * scripted numbers here are mock data: tokens are always marked as estimated.
 */
import type {
  Approval,
  Budget,
  Choice,
  DoneEvent,
  Event,
  Manifest,
  Phase,
  Plan,
  StepEvent,
} from "../../contract/events.ts";

export type Draft<E extends Event> = E extends Event ? Omit<E, "id" | "run_id" | "ts" | "budget"> & { budget?: Budget | null } : never;

export const RUN_LIMITS = { max_steps: 25, max_tokens: 150_000, max_seconds: 600 } as const;

export function budget(steps: number, seconds: number): Budget {
  return {
    steps,
    max_steps: RUN_LIMITS.max_steps,
    tokens: steps * 900,
    max_tokens: RUN_LIMITS.max_tokens,
    tokens_reported: false,
    prompt_tokens: 0,
    completion_tokens: 0,
    seconds,
    max_seconds: RUN_LIMITS.max_seconds,
  };
}

export const MANIFEST: Manifest = {
  read_paths: ["README.md", "invoice_calc/**", "tests/**", "pytest.ini"],
  write_paths: ["invoice_calc/**"],
  commands: ["pytest", "python", "ls", "cat"],
  network: [],
  may_edit_tests: false,
};

export function plan(risk: Plan["risk"], reasons: string[], manifest: Manifest = MANIFEST): Plan {
  return {
    steps: [
      "Read the README and the code to understand the setup",
      "Run the tests to see the failure",
      "Fix the discount arithmetic in invoice_calc/invoice.py",
      "Run the tests again",
    ],
    done_criteria: ["The failing discount test passes", "No other test breaks"],
    manifest,
    risk,
    risk_reasons: reasons,
    planner_risk: "low",
    edited_by_human: false,
    clipped: [],
  };
}

const NO_BREAKER = { denials: 0, injection_denials: 0, trips: 0 };

export interface StepSpec {
  step: number;
  tool: string;
  args: StepEvent["args"];
  reason: string;
  decision?: StepEvent["decision"];
  rule_id?: string;
  rule_reason?: string;
  risk?: StepEvent["risk"];
  blocked_sentence?: string | null;
  executed?: boolean;
  approved?: boolean | null;
  output_preview?: string;
  checkpoint?: number | null;
  tags?: string[];
  source?: string | null;
  taint_sources?: string[];
  breaker?: StepEvent["breaker"];
  seconds: number;
}

export function step(spec: StepSpec): Draft<StepEvent> {
  const decision = spec.decision ?? "allow";
  const ruleReason = spec.rule_reason ?? "Files inside the workspace are allowed.";
  const ruleId = spec.rule_id ?? "path.allow_workspace";
  const approved = spec.approved ?? null;
  const executed = spec.executed ?? decision === "allow";
  const blocked = decision === "deny" || (decision === "ask" && approved !== true);
  return {
    kind: "step",
    phase: "act",
    budget: budget(spec.step, spec.seconds),
    step: spec.step,
    guard: true,
    tool: spec.tool,
    args: spec.args,
    agent_reason: spec.reason,
    decision,
    rule_id: ruleId,
    reason: ruleReason,
    risk: spec.risk ?? "low",
    tags: spec.tags ?? [],
    source: spec.source ?? null,
    taint_sources: spec.taint_sources ?? [],
    matched_rules: [{ rule_id: ruleId, source: "rulebook", decision, risk: spec.risk ?? "low", reason: ruleReason }],
    in_plan: true,
    observed: false,
    approved,
    executed,
    blocked,
    blocked_sentence: blocked ? (spec.blocked_sentence ?? null) : null,
    output_preview: spec.output_preview ?? "",
    checkpoint: spec.checkpoint ?? null,
    masked_secrets: false,
    reader: null,
    breaker: spec.breaker ?? NO_BREAKER,
  };
}

export function choice(id: string, label: string, approves: boolean, primary: boolean, shortcut: string | null): Choice {
  return { id, label, approves, primary, shortcut };
}

export interface ApprovalSpec {
  id: string;
  kind: Approval["kind"];
  step: number;
  phase: Phase;
  eyebrow: string;
  headline: string;
  body: Approval["body"];
  note?: string | null;
  choices: Choice[];
  free_text?: Approval["free_text"];
  layout?: Approval["layout"];
  on_timeout: string;
  timeout_text: string;
  timeout_seconds?: number;
}

export function approval(spec: ApprovalSpec): Approval {
  return {
    id: spec.id,
    kind: spec.kind,
    step: spec.step,
    phase: spec.phase,
    eyebrow: spec.eyebrow,
    headline: spec.headline,
    body: spec.body,
    note: spec.note ?? null,
    choices: spec.choices,
    free_text: spec.free_text ?? null,
    layout: spec.layout ?? "standard",
    timeout_seconds: spec.timeout_seconds ?? 120,
    on_timeout: spec.on_timeout,
    timeout_text: spec.timeout_text,
    action: null,
  };
}

export const DENY_CHOICES = (approveLabel: string, approveId = "allow"): Choice[] => [
  choice(approveId, approveLabel, true, true, "A"),
  choice("deny", "Deny", false, false, "D"),
];

export function doneDraft(base: Partial<DoneEvent> & Pick<DoneEvent, "status" | "summary">): Draft<DoneEvent> {
  return {
    kind: "done",
    phase: "learn",
    budget: null,
    steps: 0,
    guard: true,
    provider: "fake",
    model: "scripted",
    sandbox_runtime: "runc",
    counts: { allowed: 0, denied: 0, asked: 0, injection_tagged: 0 },
    plan: null,
    verified: null,
    retries: 0,
    breaker_trips: 0,
    rollbacks: 0,
    checkpoints: 0,
    delivered: false,
    delivery_blocked: false,
    released_by_human: false,
    changed_files: [],
    final_tests: null,
    scout_flags: [],
    suspicious_pages: [],
    trifecta: null,
    ...base,
  };
}
