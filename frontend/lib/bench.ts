import type { BenchModeResult, BenchResults } from "./api";

export interface BenchTotals {
  total: number;
  baselineAttacks: number;
  fullAttacks: number;
  tasksFinished: number;
}

/** Per-scenario results for one scenario id, when both modes were run. */
export function resultFor(results: BenchResults | null, id: string): { baseline: BenchModeResult; full: BenchModeResult } | null {
  const row = results?.scenarios.find((s) => s.id === id);
  return row?.baseline && row.full ? { baseline: row.baseline, full: row.full } : null;
}

/** Totals across the scenarios that have results, out of the catalogue's total. */
export function benchTotals(results: BenchResults, total: number): BenchTotals {
  let baselineAttacks = 0;
  let fullAttacks = 0;
  let tasksFinished = 0;
  for (const s of results.scenarios) {
    if (s.baseline?.attack_succeeded) baselineAttacks += 1;
    if (s.full?.attack_succeeded) fullAttacks += 1;
    if (s.full?.task_completed) tasksFinished += 1;
  }
  return { total, baselineAttacks, fullAttacks, tasksFinished };
}

export interface ModeRates {
  attackSuccess: number | null;
  taskCompletion: number | null;
}

function rate(hits: number, of: number): number | null {
  return of === 0 ? null : Math.round((hits / of) * 100);
}

/** Attack success and task completion, as whole percentages, for each mode. */
export function modeRates(results: BenchResults, mode: "baseline" | "full", category?: string): ModeRates {
  const rows = results.scenarios.filter((s) => (category === undefined || s.category === category) && s[mode] !== null);
  const attacks = rows.filter((s) => s[mode]?.attack_succeeded).length;
  const tasks = rows.filter((s) => s[mode]?.task_completed).length;
  return { attackSuccess: rate(attacks, rows.length), taskCompletion: rate(tasks, rows.length) };
}

export function categoriesIn(results: BenchResults): string[] {
  return [...new Set(results.scenarios.map((s) => s.category))];
}
