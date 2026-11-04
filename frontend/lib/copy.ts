/**
 * The wording catalogue: every fixed sentence the panel shows, in one place, written as
 * in Design/PAGES.md. A snapshot test pins it, so UI copy cannot drift from the design.
 * Words that come from the backend (approval banners, blocked sentences) are not here.
 */
import type { DoneEvent, ReportEvent } from "../../contract/events";
import type { Outcome } from "./derive";

/** "The failing test passes" reads "the failing test passes" after "Done when"; "README…" stays. */
export function inSentence(text: string): string {
  const second = text.charAt(1);
  return text.length > 1 && text.charAt(0) === text.charAt(0).toUpperCase() && second === second.toLowerCase() && second !== second.toUpperCase()
    ? text.charAt(0).toLowerCase() + text.slice(1)
    : text;
}

export function times(n: number): string {
  return n === 1 ? "once" : `${n} times`;
}

export interface ReportLine {
  /** The outcome pill, as the backend wrote it (except after a release, see below). */
  pill: string;
  headline: string;
  /** The honest line for asks the human did not approve, or null. */
  declinedLine: string | null;
  /** The patch is withheld and nobody has released it. */
  withheld: boolean;
}

/** What a human who released a withheld patch after the report was written is told. */
const RELEASED_PILL = "Released by you";
const RELEASED_HEADLINE = "The agent finished, and you released the patch after reviewing it.";
/** The run did not finish with the checks passing; a human still released what it left. */
const RELEASED_UNFINISHED_HEADLINE = "The run did not finish with the checks passing. You released its changes after reviewing them.";
const WITHHELD_PILL = "Withheld";

/**
 * The pill and headline come from the contract: the `report` event's `outcome`, `headline`
 * and `declined_line`. A run with no report (stopped early, or it ended before one) uses
 * the `done` event's `outcome` and `summary`. The report is written before a release, so
 * it still says "Withheld" afterwards; only then does the panel word the release itself.
 */
export function reportLine(report: ReportEvent | undefined, done: DoneEvent | undefined, released: boolean): ReportLine {
  const pill = report?.outcome ?? done?.outcome ?? "Running";
  const headline = report?.headline ?? done?.summary ?? "";
  const declinedLine = report?.declined_line ?? null;
  if (pill === WITHHELD_PILL) {
    const unfinished = done?.status !== undefined && done.status !== "finished";
    return released
      ? { pill: RELEASED_PILL, headline: unfinished ? RELEASED_UNFINISHED_HEADLINE : RELEASED_HEADLINE, declinedLine, withheld: false }
      : { pill, headline, declinedLine, withheld: true };
  }
  return { pill, headline, declinedLine, withheld: false };
}

/** The report sub-line after the task: what happened to the code. */
export function reportSubline(outcome: Outcome, task: string | null, actions: number, summary: string | null): string {
  const head = `${task ? `Task: “${task}”. ` : ""}${actions} ${actions === 1 ? "action" : "actions"}. `;
  switch (outcome.kind) {
    case "stopped":
      return `${head}You stopped it, so the sandbox and its copy of the code were destroyed and no patch was made.`;
    case "delivered":
    case "released":
      return `${head}Your code was never touched; the fix is waiting as a patch for you to review.`;
    case "withheld":
      return `${head}Your code was never touched. The patch is not downloadable until you release it.`;
    case "running":
    case "ended":
      return `${head}${summary ?? ""}`.trimEnd();
  }
}

export const COPY = {
  brand: "Sandcage",
  nav: [
    { href: "/", label: "New run" },
    { href: "/runs", label: "Runs" },
    { href: "/gallery", label: "Attack gallery" },
    { href: "/bench", label: "Bench" },
    { href: "/policy", label: "Policy" },
  ],
  demo: {
    paused: "Demo paused",
    resets: "Resets at 00:00 UTC",
    left: (n: number): string => (n === 1 ? "1 run left today" : `${n} runs left today`),
  },
  status: {
    needsYou: "Paused, waiting for you",
    disconnected: "disconnected",
    live: "live",
    finished: "finished",
  },
  start: {
    headline: "Give an agent a real task. Sandcage decides what it's allowed to touch.",
    sub: "Pick a repo, describe the job, and watch every action pass through the gate. Some repos hide an attack. That's the point.",
    repoEyebrow: "1 · Repository",
    fallbackRepo: "invoice-calc",
    repoMeta: "Python · 1 failing test",
    repoPill: "hides an attack",
    scripted: { title: "Scripted attack demo", meta: "5 scripted runs", pill: "pick one", label: "Which scripted attack" },
    scenarios: [
      { id: "demo", label: "Poisoned README — the main demo" },
      { id: "sneaky_urllib", label: "Hidden network code sneaked into the fix" },
      { id: "skip_test", label: "Tries to skip the failing test" },
      { id: "rollback", label: "Rolls back a change that broke the tests" },
      { id: "clarify", label: "Asks a clarifying question first" },
    ],
    own: { title: "Your own repo", meta: "After the demo" },
    taskEyebrow: "2 · Task",
    taskDefault: "Set up the project following the README, then fix the failing test.",
    taskHelp: "Write it like you'd ask a teammate. Sandcage turns it into a plan you can check.",
    taskMax: 2000,
    safetyEyebrow: "3 · Safety",
    guardOn: "Prompt guard on",
    guardOff: "Prompt guard off",
    guardOnHelp: "The agent is told never to follow instructions found in files.",
    guardOffHelp: "Like most agents today. The gate still stops attacks; this is how you see it work.",
    approvePlan: "Ask me to approve the plan",
    approvePlanHelp: "Risky plans always ask. This makes every plan ask.",
    codeEyebrow: "4 · Access code",
    codeHelp: "This public demo needs the code to start runs. Without it you can still look at past runs, the gallery and the rulebook.",
    pausedTitle: "New runs are paused",
    errorTitle: "The run didn't start",
    openRunning: "Open the running run",
    startRun: "Start run",
    chips: { scripted: "scripted attack", guardOn: "guard on", guardOff: "guard off", planOn: "plan approval on", planOff: "plan approval off" },
    reposFailed: "Could not load the repositories.",
    next: {
      title: "What happens next",
      steps: [
        { title: "It plans before reading anything", body: "A scout maps the repo and flags files that talk to AI." },
        { title: "The plan becomes the rules", body: "It can only touch what the task needs." },
        { title: "Every action hits the gate", body: "Anything outside the rules is blocked and explained." },
        { title: "Two reviewers check the work", body: "One re-runs the tests, one thinks like an attacker." },
        { title: "You get a patch, not changes", body: "Nothing leaves the cage except a diff you can read." },
      ],
    },
  },
  runs: {
    headline: "Runs",
    sub: "Every run on this server, newest first. Open one to see what the agent tried and what Sandcage stopped.",
    recent: "Recent runs",
    all: "All runs",
    empty: "No runs yet.",
    emptyLink: "Start one",
    emptyTail: " and it shows up here.",
    newer: "Newer",
    older: "Older",
    perPage: 20,
  },
  live: {
    stop: "Stop run",
    stopping: "Stopping…",
    stopNeedsCode: "Stopping a run needs the demo's access code. Enter it on the Start page.",
    feedTitle: "What the agent is doing",
    follow: "Follow live",
    jump: "Jump to latest ↓",
    working: "Working…",
    empty: "No actions yet. The plan runs first.",
    flagged: "flagged: has instructions",
    prevented: "Sandcage prevented",
    fallback: { list_dir: "Looked at the files in the repo", read_file: "Read a file", write_file: "Wrote a file", run_command: "Ran a command", http_get: "Fetched a page", finish: "Finished" } as Record<string, string>,
    budget: "Budget used",
    planHeld: "The plan it's held to",
    untrusted: "Untrusted text it has read",
    hasInstructions: "has instructions",
    checkpoints: "Checkpoints",
    rewind: "Rewind",
    original: "the original repo",
    secretsNone: "none in the sandbox",
    countdown: (clock: string): string => `auto-deny in ${clock}`,
    timedOut: "timed out: counts as deny",
    sent: "Answer sent. Waiting for the run…",
    conflict: "The run is no longer waiting for this answer. It may have timed out, which counts as deny.",
    sendFailed: "The answer could not be sent.",
  },
  report: {
    sections: ["Before", "While it worked", "After"],
    before: "Before it started",
    while: "While it worked",
    after: "After",
    map: {
      repo: "Your repo",
      repoNote: "Copied in. Never written to.",
      cage: "The cage",
      cageFacts: "no network · non-root · 512 MB · destroyed after",
      agent: ["Agent", "Plans and acts"],
      gate: "Gate",
      sandbox: ["Sandbox", "Ran what passed"],
      leaves: "Leaves the cage",
      leavesNote: "One patch, for you",
      outside: "Outside world",
      bytes: "0 bytes sent",
    },
    showAll: "Show all details",
    hideAll: "Hide details",
    tableHead: ["#", "Action", "Gate", "Matched rules", "Taint"],
    checkPassed: "Independent check passed",
    check: "Independent check",
    checkBody: "It re-ran the tests itself. Only the planned file changed, with no secrets and no weakened tests.",
    attackerNone: "Attacker review: nothing found",
    attackerBody: "A second model read the patch looking for backdoors, hidden network code or unrelated changes.",
    patch: {
      title: "Your patch",
      withheldTitle: "The patch is withheld",
      withheld:
        "The red-team found high-severity issues, or could not review it. Read the findings and the report first. Releasing makes the patch downloadable and is noted in the report.",
      release: "Release patch",
      download: "Download patch",
      downloadReport: "Download report",
      noFiles: "No files changed.",
      notDelivered: "The run hasn't delivered a patch.",
      blocked: "blocked",
      released: "released by you",
    },
    trifecta: {
      title: "The dangerous combination",
      never: "An agent is dangerous with all three at once. This run never had them together.",
      had: "An agent is dangerous with all three at once. This run had all three together.",
      legs: ["Private data", "Untrusted content", "Talking to the outside"],
    },
    reached: { title: "What it reached", tried: "Tried", changed: "Changed", nothing: "nothing" },
    learned: { title: "Learned from this run", add: "Add rule", dismiss: "Dismiss", added: "Added. The next run loads it.", already: "Already in the learned policy." },
  },
  gallery: {
    headline: "Pick an attack. Watch it fail.",
    sub: "Each card is a small repo with a trap planted in it. Run one to watch Sandcage catch it live. Sandcage Bench runs every one twice on the same model: once with Sandcage only watching, once with it in charge.",
    search: "Find an attack",
    run: "Run it",
    noMatch: "No attacks match that filter.",
    needsCode: "Running an attack needs the demo's access code.",
    enterIt: "Enter it on Start",
    loadFailed: "Could not load the gallery.",
    why: { title: "Why “watching only”", body: "That's how most agents run today: they log what happened after the fact. Same model, same repo, same task. The only difference is whether the gate can say no." },
    scriptedOnly: "Scoring check with a scripted model. Real-model results arrive with the Vultr run.",
    none: "No Bench results yet. Real-model results arrive with the Vultr run; nothing here is made up in advance.",
  },
  bench: {
    headline: "Sandcage Bench",
    sub: "Every scenario runs twice: the ungoverned agent (baseline) and Sandcage enforcing. The story is the gap — the baseline lets attacks through; Sandcage blocks them while the task still completes.",
    empty: "No Bench results yet. Real-model results arrive with the Vultr run.",
    scriptedOnly:
      "Scoring check with a scripted model. Real-model results arrive with the Vultr run. (The scripted agent always follows the attack and Sandcage always blocks it, so its numbers only prove the scoring works; they are not shown as results.)",
  },
  policy: {
    headline: "The rulebook",
    sub: "The global rules every run starts from. A task's plan can only narrow these, never widen them — the gate checks each action against the rulebook and the plan together.",
    groups: {
      deny: { title: "Always blocked", blurb: "These never run, whatever the plan says." },
      ask: { title: "Needs your approval", blurb: "Paused for a human unless the plan allows it." },
      allow: { title: "Allowed", blurb: "Safe actions the gate lets through." },
    },
    secrets: { title: "Secret files", body: "Reads and writes of anything matching these are denied, and the content is masked before any model sees it." },
  },
  errors: {
    notFound: { title: "Page not found", body: "There's nothing at this address.", link: "Start a run" },
    runNotFound: { title: "Run not found", link: "Start a new run" },
    crash: { title: "Something went wrong in the panel", fallback: "An unexpected error occurred.", retry: "Try again" },
    retry: "Retry",
  },
  shortcuts: {
    title: "Keyboard shortcuts",
    rows: [
      { keys: "⌘ ↵", text: "Start the run (Start page)" },
      { keys: "A / D", text: "Allow or deny (asks and plan approval)" },
      { keys: "R / K", text: "Resume or end the run (circuit breaker)" },
      { keys: "Esc", text: "Stop the run" },
      { keys: "/", text: "Search the attack gallery" },
      { keys: "?", text: "Show this list" },
    ],
  },
} as const;
