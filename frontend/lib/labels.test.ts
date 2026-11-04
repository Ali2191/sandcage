import { describe, expect, it } from "vitest";
import type { RunRow } from "./api";
import { isRealBench } from "./api";
import { modelLabel, repoLabel, runMeta, runOutcome } from "./labels";

const row = (patch: Partial<RunRow>): RunRow => ({
  id: "3f2a9c1e7b04", repo: "invoice-calc", task: "t", guard: true, status: "finished", created: 1793000000, finished: 1793000060,
  provider: "fake", model: "scripted", steps: 9, counts: { allowed: 7, denied: 2, asked: 0, injection_tagged: 2 }, tokens: null, tokens_reported: false,
  outcome: "Delivered", delivered: true, sandbox_runtime: "runc", released_by_human: false, delivery_blocked: false, ...patch,
});

describe("labels", () => {
  it("labels repos", () => {
    expect(repoLabel("invoice-calc")).toBe("invoice-calc");
    expect(repoLabel("bench:readme-exfil")).toBe("Bench · readme-exfil");
  });

  it("labels models", () => {
    expect(modelLabel({ provider: "fake", model: "scripted" })).toBe("scripted model");
    expect(modelLabel({ provider: "vultr", model: "deepseek-v4.1-flash-1017" })).toBe("deepseek-v4.1-flash-1017");
    expect(modelLabel({ provider: null, model: null })).toBeNull();
  });

  it("words the outcome of a run row", () => {
    expect(runOutcome(row({ status: "running" }))).toEqual({ text: "running…", tone: "muted" });
    expect(runOutcome(row({ status: "stopped" }))).toEqual({ text: "stopped", tone: "muted" });
    expect(runOutcome(row({}))).toEqual({ text: "2 blocked", tone: "ember" });
    expect(runOutcome(row({ counts: { allowed: 9, denied: 0, asked: 0, injection_tagged: 0 } }))).toEqual({ text: "clean", tone: "muted" });
    expect(runOutcome(row({ status: "verify_failed", counts: null, outcome: "Not verified" }))).toEqual({ text: "Not verified", tone: "plain" });
    expect(runOutcome(row({ status: "plan_rejected", outcome: "Plan rejected" }))).toEqual({ text: "Plan rejected", tone: "plain" });
    expect(runOutcome(row({ status: "verify_failed", outcome: "Withheld" }))).toEqual({ text: "Withheld", tone: "ember" });
    expect(runOutcome(row({ status: "error", outcome: null }))).toEqual({ text: "Ended", tone: "plain" });
  });

  it("builds the meta line and leaves out what is missing", () => {
    expect(runMeta(row({}), false)).toBe("3f2a9c · 9 steps · scripted model");
    expect(runMeta(row({ provider: null, model: null, steps: 1 }), false)).toBe("3f2a9c · 1 step");
    expect(runMeta(row({}), true)).toMatch(/^3f2a9c · 9 steps · scripted model · /);
  });

  it("counts a Bench result as real only for a real model", () => {
    const base = { date: null, scenarios: [] };
    expect(isRealBench({ ...base, provider: "fake", model: "scripted" })).toBe(false);
    expect(isRealBench({ ...base, provider: "vultr", model: null })).toBe(false);
    expect(isRealBench({ ...base, provider: "vultr", model: "glm-5.3" })).toBe(true);
    expect(isRealBench(null)).toBe(false);
  });
});
