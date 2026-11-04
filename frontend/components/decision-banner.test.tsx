// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import type { Approval, ApprovalKind, Event } from "@contract";
import { APPROVAL_KINDS } from "@contract";
import { approval, choice, DENY_CHOICES } from "../mock/builders";
import { DecisionBanner } from "./decision-banner";
import type { ApprovalState } from "./use-approval";

afterEach(cleanup);

const sample = (): Approval[] =>
  readFileSync(path.join(import.meta.dirname, "../../contract/samples/demo-run.jsonl"), "utf8")
    .split("\n")
    .filter((l) => l.trim() !== "")
    .map((l) => JSON.parse(l) as Event)
    .flatMap((e) => (e.kind === "approval_needed" ? [e.approval] : []));

/** One approval record per kind the contract defines. A new kind with no entry fails the first test. */
const FIXTURES: Record<ApprovalKind, Approval> = {
  plan: sample().find((a) => a.kind === "plan") as Approval,
  breaker: sample().find((a) => a.kind === "breaker") as Approval,
  action: approval({
    id: "action:3", kind: "action", step: 3, phase: "act", eyebrow: "NEEDS YOUR CALL",
    headline: "The agent wants to do something the plan doesn't cover.",
    body: [{ type: "code", text: "$ pip install requests" }, { type: "text", text: "Installing packages is not in the plan.", muted: false }],
    choices: DENY_CHOICES("Allow once"), on_timeout: "deny", timeout_text: "timed out: counts as deny",
  }),
  amendment: approval({
    id: "amendment:5", kind: "amendment", step: 5, phase: "approve", eyebrow: "NEEDS YOUR CALL",
    headline: "The agent wants to extend the locked plan.",
    body: [{ type: "facts", rows: [{ label: "Adds", value: "write setup.cfg", mono: true }] }],
    choices: DENY_CHOICES("Allow once"), on_timeout: "deny", timeout_text: "timed out: counts as deny",
  }),
  redteam: approval({
    id: "redteam:0", kind: "redteam", step: 6, phase: "redteam", eyebrow: "RED-TEAM",
    headline: "The red-team found high-severity issues. Release the patch anyway?",
    body: [{ type: "findings", items: [{ location: "invoice_calc/invoice.py:3", text: "Posts each order to an outside address." }] }],
    choices: [choice("keep", "Keep it blocked", false, true, null), choice("release", "Release patch", true, false, null)],
    on_timeout: "keep", timeout_text: "timed out: counts as keep it blocked",
  }),
  question: approval({
    id: "question:0", kind: "question", step: 0, phase: "understand", eyebrow: "A QUESTION BEFORE THE AGENT STARTS",
    headline: "Is the discount a percentage off or a fixed amount off?", body: [],
    choices: [choice("answer", "Answer", true, true, null), choice("stop", "Stop the run", false, false, null)],
    free_text: { label: "Is the discount a percentage off or a fixed amount off?", placeholder: "Your answer", max_length: 1000, required: true },
    layout: "question", on_timeout: "stop", timeout_text: "timed out: counts as stop",
  }),
};

const IDLE: ApprovalState = { sending: null, sent: false, error: null };
const renderBanner = (a: Approval, onDecide = vi.fn(), state = IDLE) =>
  render(<DecisionBanner approval={a} deadlineMs={Date.now() + a.timeout_seconds * 1000} state={state} onDecide={onDecide} />);

describe("DecisionBanner renders every approval kind from the one record", () => {
  it("has a fixture for every kind in the contract", () => {
    expect(Object.keys(FIXTURES).sort()).toEqual([...APPROVAL_KINDS].sort());
  });

  it.each(APPROVAL_KINDS)("%s: eyebrow, headline and every choice", (kind) => {
    const a = FIXTURES[kind];
    renderBanner(a);
    expect(screen.getByText(a.eyebrow)).toBeTruthy();
    expect(screen.getAllByText(a.headline).length).toBeGreaterThan(0);
    for (const c of a.choices) expect(screen.getByRole("button", { name: new RegExp(c.label) })).toBeTruthy();
    expect(document.querySelector(`[data-approval-kind="${kind}"]`)).not.toBeNull();
  });

  it.each(APPROVAL_KINDS)("%s: shows the countdown with the timeout's choice", (kind) => {
    const a = FIXTURES[kind];
    renderBanner(a);
    const word = a.choices.find((c) => c.id === a.on_timeout)?.label.split(" ")[0]?.toLowerCase() ?? "";
    expect(screen.getByTestId("countdown").textContent).toMatch(new RegExp(`^auto-${word} in 2:00$`));
  });
});

describe("shortcuts come from the record", () => {
  it("answers with the choice whose shortcut was pressed", () => {
    const onDecide = vi.fn();
    renderBanner(FIXTURES.plan, onDecide);
    fireEvent.keyDown(window, { key: "a" });
    expect(onDecide).toHaveBeenCalledWith("approve", undefined);
  });

  it("uses R and K for the breaker", () => {
    const onDecide = vi.fn();
    renderBanner(FIXTURES.breaker, onDecide);
    fireEvent.keyDown(window, { key: "k" });
    expect(onDecide).toHaveBeenCalledWith("end", undefined);
  });

  it("has no shortcut for the red-team release, so a key press does nothing", () => {
    const onDecide = vi.fn();
    renderBanner(FIXTURES.redteam, onDecide);
    for (const key of ["a", "d", "r", "k", "y", "Enter"]) fireEvent.keyDown(window, { key });
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("ignores keys while typing in a box", () => {
    const onDecide = vi.fn();
    renderBanner(FIXTURES.question, onDecide);
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "a" });
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("ignores keys once an answer was sent", () => {
    const onDecide = vi.fn();
    renderBanner(FIXTURES.plan, onDecide, { sending: null, sent: true, error: null });
    fireEvent.keyDown(window, { key: "a" });
    expect(onDecide).not.toHaveBeenCalled();
    expect(screen.getByText("Answer sent. Waiting for the run…")).toBeTruthy();
  });
});

describe("the free-text question", () => {
  it("needs text before Answer works, then sends it", () => {
    const onDecide = vi.fn();
    renderBanner(FIXTURES.question, onDecide);
    const answer = screen.getByRole("button", { name: "Answer" }) as HTMLButtonElement;
    expect(answer.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "  A percentage off.  " } });
    expect(answer.disabled).toBe(false);
    fireEvent.click(answer);
    expect(onDecide).toHaveBeenCalledWith("answer", "A percentage off.");
  });

  it("lets Stop the run through without text", () => {
    const onDecide = vi.fn();
    renderBanner(FIXTURES.question, onDecide);
    fireEvent.click(screen.getByRole("button", { name: "Stop the run" }));
    expect(onDecide).toHaveBeenCalledWith("stop", undefined);
  });
});

describe("states", () => {
  it("shows a send failure in the banner", () => {
    renderBanner(FIXTURES.action, vi.fn(), { sending: null, sent: false, error: "The answer could not be sent." });
    expect(screen.getByText("The answer could not be sent.")).toBeTruthy();
  });

  it("disables every button while an answer is being sent", () => {
    renderBanner(FIXTURES.action, vi.fn(), { sending: "allow", sent: false, error: null });
    for (const b of screen.getAllByRole("button")) expect((b as HTMLButtonElement).disabled).toBe(true);
  });
});
