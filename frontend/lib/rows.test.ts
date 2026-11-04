import { describe, expect, it } from "vitest";
import type { StepEvent } from "@contract";
import { actionLine, allowedSentence, capitalise, readFlagged, ruleLine, stepNumber } from "./rows";

const base = {
  kind: "step", step: 1, tool: "read_file", args: { path: "README.md" }, agent_reason: "read the setup steps", source: null, rule_id: "path.allow_workspace",
} as unknown as StepEvent;
const step = (patch: Partial<StepEvent>): StepEvent => ({ ...base, ...patch }) as StepEvent;

describe("rows", () => {
  it("writes the raw action as one line", () => {
    expect(actionLine(step({ tool: "run_command", args: { command: "pytest -q" } }))).toBe("$ pytest -q");
    expect(actionLine(step({}))).toBe("read_file README.md");
    expect(actionLine(step({ tool: "list_dir", args: {} }))).toBe("list_dir .");
    expect(actionLine(step({ tool: "http_get", args: { url: "https://docs.example.org/x" } }))).toBe("http_get https://docs.example.org/x");
    expect(actionLine(step({ tool: "finish", args: {} }))).toBe("finish");
  });

  it("capitalises the agent's reason for allowed rows, with a tool fallback", () => {
    expect(allowedSentence(step({}))).toBe("Read the setup steps");
    expect(allowedSentence(step({ agent_reason: "  " }))).toBe("Read a file");
    expect(allowedSentence(step({ agent_reason: "", tool: "list_dir" }))).toBe("Looked at the files in the repo");
    expect(allowedSentence(step({ agent_reason: "", tool: "mystery" }))).toBe("Did something");
    expect(capitalise("")).toBe("");
  });

  it("shows the rule and where the attack was traced to", () => {
    expect(ruleLine(step({ source: "README.md", rule_id: "net.deny_tools" }))).toBe("net.deny_tools · traced to README.md");
    expect(ruleLine(step({ rule_id: "net.deny_tools" }))).toBe("net.deny_tools");
  });

  it("flags a read of a file the scout flagged, and only reads", () => {
    expect(readFlagged(step({}), ["README.md"])).toBe(true);
    expect(readFlagged(step({}), ["other.md"])).toBe(false);
    expect(readFlagged(step({ tool: "write_file" }), ["README.md"])).toBe(false);
  });

  it("pads step numbers", () => {
    expect(stepNumber(3)).toBe("03");
    expect(stepNumber(12)).toBe("12");
  });
});
