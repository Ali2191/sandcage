/** Pure helpers that turn a step event into the text of a feed or report row. */
import type { StepEvent } from "@contract";
import { COPY } from "./copy";

export function capitalise(text: string): string {
  const trimmed = text.trim();
  return trimmed === "" ? trimmed : trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
}

function arg(step: StepEvent, key: string): string | null {
  const value = step.args[key];
  return typeof value === "string" && value !== "" ? value : null;
}

/** The raw action as one mono line: "$ pytest -q", "read_file README.md", … */
export function actionLine(step: StepEvent): string {
  switch (step.tool) {
    case "run_command":
      return `$ ${arg(step, "command") ?? ""}`.trimEnd();
    case "read_file":
    case "write_file":
    case "list_dir":
      return `${step.tool} ${arg(step, "path") ?? "."}`;
    case "http_get":
      return `http_get ${arg(step, "url") ?? ""}`.trimEnd();
    case "finish":
      return "finish";
    default:
      return step.tool;
  }
}

/** The plain sentence of an allowed row: the agent's stated reason, or a fallback for the tool. */
export function allowedSentence(step: StepEvent): string {
  const reason = capitalise(step.agent_reason);
  if (reason !== "") return reason;
  return COPY.live.fallback[step.tool] ?? "Did something";
}

/** "traced to README.md" part of a blocked row's third line. */
export function ruleLine(step: StepEvent): string {
  return step.source !== null ? `${step.rule_id} · traced to ${step.source}` : step.rule_id;
}

/** hh:mm:ss in the viewer's time zone from Unix seconds. */
export function timeLabel(ts: number): string {
  const d = new Date(ts * 1000);
  const two = (n: number): string => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

/** Whether a file read carried instructions aimed at an AI (the scout flagged the file). */
export function readFlagged(step: StepEvent, flagged: readonly string[]): boolean {
  const path = arg(step, "path");
  return step.tool === "read_file" && path !== null && flagged.includes(path);
}

/** The zero-padded step number of the report list: 3 -> "03". */
export function stepNumber(n: number): string {
  return String(n).padStart(2, "0");
}
