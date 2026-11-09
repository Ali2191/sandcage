import type { RunRow } from "./api";

/** "bench:<id>" reads "Bench · <id>"; a demo repo is its own name. */
export function repoLabel(repo: string): string {
  return repo.startsWith("bench:") ? `Bench · ${repo.slice(6)}` : repo;
}

/** Provider names whose runs follow a script: no model is called. */
const SCRIPTED_PROVIDERS: ReadonlySet<string> = new Set(["fake", "scripted"]);

/**
 * The one rule for showing a run's model: "scripted model" for a scripted run (whatever model
 * name came with it), the model name for a real one, null when unknown. A scripted run must
 * never read as a real model's behaviour.
 */
export function modelLabel(row: Pick<RunRow, "provider" | "model">): string | null {
  if (row.provider !== null && SCRIPTED_PROVIDERS.has(row.provider)) return "scripted model";
  return row.model !== null && row.model !== "" ? row.model : null;
}

export type RunOutcome = { text: string; tone: "muted" | "ember" | "plain" };

/** The outcome shown at the right of a run row. */
export function runOutcome(row: Pick<RunRow, "status" | "counts" | "outcome">): RunOutcome {
  if (row.status === "running") return { text: "running…", tone: "muted" };
  if (row.status === "stopped") return { text: "stopped", tone: "muted" };
  if (row.status === "finished") {
    const blocked = row.counts ? row.counts.denied : 0;
    return blocked > 0 ? { text: `${blocked} blocked`, tone: "ember" } : { text: "clean", tone: "muted" };
  }
  // Any other end: the backend's own label ("Not verified", "Plan rejected", ...), never the raw status.
  return { text: row.outcome ?? "Ended", tone: row.outcome === "Withheld" ? "ember" : "plain" };
}

/** "6 chars · n steps · model · date" for a run row (parts that are missing are left out). */
export function runMeta(row: RunRow, withDate: boolean): string {
  const parts = [row.id.slice(0, 6), `${row.steps} ${row.steps === 1 ? "step" : "steps"}`];
  const model = modelLabel(row);
  if (model) parts.push(model);
  if (withDate) {
    parts.push(
      new Date(row.created * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }),
    );
  }
  return parts.join(" · ");
}
