import { describe, expect, it } from "vitest";
import type { BenchResults } from "./api";
import { benchTotals, categoriesIn, modeRates, resultFor } from "./bench";

const results: BenchResults = {
  provider: "vultr",
  model: "glm-5.3",
  date: "2026-11-06",
  scenarios: [
    { id: "a", category: "leak", baseline: { attack_succeeded: true, task_completed: true }, full: { attack_succeeded: false, task_completed: true } },
    { id: "b", category: "leak", baseline: { attack_succeeded: true, task_completed: true }, full: { attack_succeeded: false, task_completed: false } },
    { id: "c", category: "cheat", baseline: { attack_succeeded: false, task_completed: true }, full: { attack_succeeded: false, task_completed: true } },
    { id: "d", category: "cheat", baseline: null, full: null },
  ],
};

describe("bench", () => {
  it("returns a result only when both modes ran", () => {
    expect(resultFor(results, "a")?.full.attack_succeeded).toBe(false);
    expect(resultFor(results, "d")).toBeNull();
    expect(resultFor(results, "zzz")).toBeNull();
    expect(resultFor(null, "a")).toBeNull();
  });

  it("totals out of the catalogue's size", () => {
    expect(benchTotals(results, 21)).toEqual({ total: 21, baselineAttacks: 2, fullAttacks: 0, tasksFinished: 2 });
  });

  it("computes whole-percent rates per mode and per category, skipping unrun scenarios", () => {
    expect(modeRates(results, "baseline")).toEqual({ attackSuccess: 67, taskCompletion: 100 });
    expect(modeRates(results, "full")).toEqual({ attackSuccess: 0, taskCompletion: 67 });
    expect(modeRates(results, "full", "leak")).toEqual({ attackSuccess: 0, taskCompletion: 50 });
    expect(modeRates(results, "full", "nothing")).toEqual({ attackSuccess: null, taskCompletion: null });
  });

  it("lists categories once, in order", () => {
    expect(categoriesIn(results)).toEqual(["leak", "cheat"]);
  });
});
