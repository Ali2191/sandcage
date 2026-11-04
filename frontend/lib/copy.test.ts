import { describe, expect, it } from "vitest";
import type { DoneEvent, ReportEvent } from "../../contract/events";
import { COPY, inSentence, reportLine, reportSubline } from "./copy";

/**
 * The wording catalogue, pinned. Each line was checked against Design/PAGES.md; a change
 * here must be a deliberate edit to the design first. `npm run wording:verify <PAGES.md>`
 * checks the catalogue against the design note itself.
 */
describe("reportLine: the pill and headline come from the contract", () => {
  const report = { outcome: "Withheld", headline: "H from the report.", declined_line: "D line." } as ReportEvent;
  const done = { outcome: "Stopped by you", summary: "S from done." } as DoneEvent;

  it("uses the report's own words", () => {
    expect(reportLine({ ...report, outcome: "Delivered" }, done, false)).toEqual({
      pill: "Delivered",
      headline: "H from the report.",
      declinedLine: "D line.",
      withheld: false,
    });
  });
  it("reads Withheld in pill and headline until a human releases", () => {
    expect(reportLine(report, done, false)).toMatchObject({ pill: "Withheld", headline: "H from the report.", withheld: true });
  });
  it("words a release made after the report was written", () => {
    expect(reportLine(report, done, true)).toMatchObject({ pill: "Released by you", withheld: false });
  });
  it("falls back to done when there is no report", () => {
    expect(reportLine(undefined, done, false)).toEqual({ pill: "Stopped by you", headline: "S from done.", declinedLine: null, withheld: false });
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
