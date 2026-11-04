import { describe, expect, it } from "vitest";
import { COPY, inSentence, outcomePill, reportHeadline, reportSubline, type Segment } from "./copy";
import type { Outcome } from "./derive";

const flat = (segments: Segment[]): string => segments.map((s) => (s.ember ? `**${s.text}**` : s.text)).join("");

/**
 * The wording catalogue, pinned. Each line was checked against Design/PAGES.md; a change
 * here must be a deliberate edit to the design first. `npm run wording:verify <PAGES.md>`
 * checks the catalogue against the design note itself.
 */
describe("report headlines (PAGES.md section 5)", () => {
  const cases: [string, Outcome, number, string][] = [
    ["finished, blocked once", { kind: "delivered" }, 1, "The agent tried to cross the line. Sandcage **blocked it once**, and the task still got done."],
    ["finished, blocked n", { kind: "delivered" }, 2, "The agent tried to cross the line. Sandcage **blocked it 2 times**, and the task still got done."],
    ["finished, nothing blocked", { kind: "delivered" }, 0, "The run finished inside the cage, and the task got done."],
    ["released, blocked n", { kind: "released" }, 3, "The agent tried to cross the line. Sandcage **blocked it 3 times**, and the task still got done."],
    ["stopped, blocked n", { kind: "stopped" }, 2, "You stopped this run. Before that, Sandcage **blocked it 2 times**."],
    ["stopped, nothing blocked", { kind: "stopped" }, 0, "You stopped this run. Nothing left the cage."],
    ["ended, blocked n", { kind: "ended", status: "verify_failed" }, 1, "Sandcage **blocked the agent once**. The run ended before the task was done."],
    ["ended, nothing blocked", { kind: "ended", status: "budget.steps" }, 0, "The run ended before the task was done."],
    ["withheld, blocked n", { kind: "withheld" }, 2, "The agent tried to cross the line. Sandcage **blocked it 2 times** and is **withholding the patch** until you review it."],
    ["withheld, nothing blocked", { kind: "withheld" }, 0, "The agent finished, but Sandcage is **withholding the patch** until you review it."],
  ];
  it.each(cases)("%s", (_name, outcome, blocked, expected) => {
    expect(flat(reportHeadline(outcome, blocked))).toBe(expected);
  });

  it("never says the task got done for a withheld or unfinished run", () => {
    for (const outcome of [{ kind: "withheld" }, { kind: "stopped" }, { kind: "ended", status: "error" }] as Outcome[]) {
      for (const blocked of [0, 1, 4]) expect(flat(reportHeadline(outcome, blocked))).not.toMatch(/task (still )?got done/);
    }
  });
});

describe("report sub-line and outcome pill", () => {
  it("words each case", () => {
    expect(reportSubline({ kind: "stopped" }, "Fix it", 4, null)).toBe(
      "Task: “Fix it”. 4 actions. You stopped it, so the sandbox and its copy of the code were destroyed and no patch was made.",
    );
    expect(reportSubline({ kind: "delivered" }, "Fix it", 1, null)).toBe(
      "Task: “Fix it”. 1 action. Your code was never touched; the fix is waiting as a patch for you to review.",
    );
    expect(reportSubline({ kind: "withheld" }, null, 5, null)).toBe("5 actions. Your code was never touched. The patch is not downloadable until you release it.");
    expect(reportSubline({ kind: "ended", status: "error" }, "Fix it", 2, "The run hit a problem.")).toBe("Task: “Fix it”. 2 actions. The run hit a problem.");
  });

  it("names the outcome", () => {
    expect(outcomePill({ kind: "delivered" })).toBe("Delivered");
    expect(outcomePill({ kind: "withheld" })).toBe("Withheld");
    expect(outcomePill({ kind: "released" })).toBe("Released by you");
    expect(outcomePill({ kind: "stopped" })).toBe("Stopped by you");
    expect(outcomePill({ kind: "ended", status: "interrupted" })).toBe("interrupted");
  });
});

describe("inSentence", () => {
  it("lowercases a leading capital but leaves acronyms and file names alone", () => {
    expect(inSentence("The failing test passes")).toBe("the failing test passes");
    expect(inSentence("README.md is untouched")).toBe("README.md is untouched");
    expect(inSentence("pytest exits 0")).toBe("pytest exits 0");
    expect(inSentence("A")).toBe("A");
    expect(inSentence("")).toBe("");
  });
});

describe("fixed wording catalogue", () => {
  it("matches the snapshot", () => {
    expect(COPY).toMatchSnapshot();
  });

  it("keeps the exact wording of the lines the demo shows", () => {
    expect(COPY.start.headline).toBe("Give an agent a real task. Sandcage decides what it's allowed to touch.");
    expect(COPY.start.taskDefault).toBe("Set up the project following the README, then fix the failing test.");
    expect(COPY.live.timedOut).toBe("timed out: counts as deny");
    expect(COPY.live.empty).toBe("No actions yet. The plan runs first.");
    expect(COPY.report.patch.withheldTitle).toBe("The patch is withheld");
    expect(COPY.gallery.headline).toBe("Pick an attack. Watch it fail.");
    expect(COPY.demo.left(1)).toBe("1 run left today");
    expect(COPY.demo.left(0)).toBe("0 runs left today");
  });
});
