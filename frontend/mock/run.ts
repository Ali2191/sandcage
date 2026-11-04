/**
 * A mock run: an append-only event log, a pending approval, and a stop flag. Scenario
 * scripts (scenarios.ts) drive it. Events are stamped here (id, run_id, ts) so a script
 * only describes what happens.
 */
import type { Approval, Choice, Event, ResolvedBy } from "../../contract/events.ts";
import type { Draft } from "./builders.ts";

export class StopSignal extends Error {}

export interface Answer {
  choice: Choice;
  by: ResolvedBy;
  text: string | null;
}

export interface RunMeta {
  id: string;
  repo: string;
  task: string;
  guard: boolean;
  approvePlan: boolean;
  scenario: string;
  created: number;
}

export interface Outputs {
  patch: string | null;
  blocked: boolean;
  released: boolean;
}

type Listener = (event: Event) => void;

export class MockRun {
  readonly meta: RunMeta;
  readonly events: Event[] = [];
  outputs: Outputs = { patch: null, blocked: false, released: false };
  private listeners = new Set<Listener>();
  private pending: { approval: Approval; settle: (a: Answer) => void } | null = null;
  private stopRequested = false;
  private nextId: () => number;
  private readonly speed: number;
  private ended = false;
  private counters = { steps: 0 };

  constructor(meta: RunMeta, nextId: () => number, speed: number) {
    this.meta = meta;
    this.nextId = nextId;
    this.speed = speed;
  }

  get finished(): boolean {
    return this.ended;
  }

  get status(): string {
    const done = this.events.find((e) => e.kind === "done");
    return done && done.kind === "done" ? done.status : "running";
  }

  get pendingApproval(): Approval | null {
    return this.pending?.approval ?? null;
  }

  get pendingApprovalId(): string | null {
    return this.pending?.approval.id ?? null;
  }

  get stepCount(): number {
    return this.counters.steps;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(draft: Draft<Event>): Event {
    this.checkStop();
    return this.append(draft);
  }

  private append(draft: Draft<Event>): Event {
    const event = {
      budget: null,
      ...draft,
      id: this.nextId(),
      run_id: this.meta.id,
      ts: Date.now() / 1000,
    } as Event;
    this.events.push(event);
    if (event.kind === "step") this.counters.steps += 1;
    if (event.kind === "done") this.ended = true;
    for (const listener of this.listeners) listener(event);
    return event;
  }

  /** Raise an approval, wait for the answer, record how it was resolved. */
  async ask(approval: Approval): Promise<Answer> {
    this.checkStop();
    this.append({ kind: "approval_needed", phase: approval.phase, approval });
    const answer = await new Promise<Answer>((settle) => {
      this.pending = { approval, settle };
      const timeout = setTimeout(() => {
        const fallback = approval.choices.find((c) => c.id === approval.on_timeout) ?? approval.choices[approval.choices.length - 1];
        if (this.pending?.approval.id === approval.id && fallback) this.answer(approval.id, fallback.id, null, "timeout");
      }, approval.timeout_seconds * 1000 * Math.max(this.speed, 0.05));
      timeout.unref();
    });
    this.append({
      kind: "approval_resolved",
      phase: approval.phase,
      approval_id: approval.id,
      approval_kind: approval.kind,
      step: approval.step,
      choice: answer.choice.id,
      approved: answer.choice.approves,
      by: answer.by,
      answer: answer.text,
    });
    this.checkStop();
    return answer;
  }

  /** Answer the pending approval. Returns false when nothing waits for that approval. */
  answer(approvalId: string, choiceId: string, text: string | null, by: ResolvedBy = "human"): boolean {
    const pending = this.pending;
    if (!pending || pending.approval.id !== approvalId) return false;
    const choice = pending.approval.choices.find((c) => c.id === choiceId);
    if (!choice) return false;
    this.pending = null;
    pending.settle({ choice, by, text });
    return true;
  }

  requestStop(): void {
    if (this.ended || this.stopRequested) return;
    this.stopRequested = true;
    const pending = this.pending;
    if (pending) {
      const no = pending.approval.choices.find((c) => !c.approves) ?? pending.approval.choices[0];
      this.pending = null;
      if (no) pending.settle({ choice: no, by: "stop", text: null });
    }
    this.wake?.();
  }

  private wake: (() => void) | null = null;

  /** Throws StopSignal once a stop was requested. Scripts call it between steps. */
  checkStop(): void {
    if (this.stopRequested && !this.ended) throw new StopSignal();
  }

  async sleep(ms: number): Promise<void> {
    this.checkStop();
    const scaled = ms * this.speed;
    if (scaled >= 1) {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, scaled);
        this.wake = () => {
          clearTimeout(timer);
          resolve();
        };
      });
      this.wake = null;
    }
    this.checkStop();
  }

  /** Record the stop: one `stopped` event, then the final `done`. Nothing is delivered. */
  recordStop(doneDraft: Draft<Event>): void {
    if (this.ended) return;
    this.stopRequested = false;
    this.append({ kind: "stopped", phase: "act", by: "human", summary: "Stopped by you.", guard: this.meta.guard });
    this.append(doneDraft);
  }

  /** Append the final event after a failure the script did not expect. */
  forceDone(doneDraft: Draft<Event>): void {
    if (!this.ended) this.append(doneDraft);
  }
}
