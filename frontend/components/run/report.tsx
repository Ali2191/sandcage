"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Event, StepEvent } from "@contract";
import { acceptSuggestion, ApiError, type RunRow } from "@/lib/api";
import { COPY, inSentence, outcomePill, reportHeadline, reportSubline, type Segment } from "@/lib/copy";
import {
  blockedLine,
  breakerText,
  changedFiles,
  checkpoints as deriveCheckpoints,
  doneEvent,
  flaggedPaths,
  gateCounts,
  hasPatchCard,
  latestBudget,
  latestPlan,
  latestVerify,
  learnEvent,
  measuredTokens,
  networkAttempts,
  outcome,
  planApprovedByHuman,
  redteam,
  reportEvent,
  sandboxRuntime,
  shortId,
  steps as stepsOf,
  clock,
  triedTargets,
  trifecta,
  trifectaLegs,
  understanding,
  blockedSinceLastResume,
} from "@/lib/derive";
import { actionLine, allowedSentence, ruleLine, stepNumber, timeLabel } from "@/lib/rows";
import { Body, Main, Side } from "../shell";
import { TopBarSlot } from "../top-bar-slot";
import { BlockedIcon, Button, Card, CheckIcon, Eyebrow, Pill, cx } from "../ui";
import { CheckpointsCard, LearnCard, ReachedCard, TrifectaCard, type LearnStatus } from "./cards";
import { DeliverPanel } from "./deliver-panel";

function Headline({ segments }: { segments: Segment[] }) {
  return (
    <h1 className="m-0 mt-5 text-[44px] font-bold leading-[1.1] tracking-tight text-head max-[960px]:text-[30px]">
      {segments.map((s, i) => (s.ember ? <span key={i} className="text-ember">{s.text}</span> : <span key={i}>{s.text}</span>))}
    </h1>
  );
}

function Tag({ children }: { children: ReactNode }) {
  return <code className="rounded bg-ember-glow px-1.5 py-0.5 font-mono text-[0.92em] text-ember-soft">{children}</code>;
}

function ContainmentMap({ events, repo: repoName }: { events: Event[]; repo: string | null }) {
  const counts = gateCounts(events);
  const runtime = sandboxRuntime(events);
  const net = networkAttempts(events);
  const repo = COPY.report.map;
  return (
    <section aria-label="Containment map" className="mt-8 rounded-card border border-line bg-[rgb(255_255_255_/_0.015)] p-6 max-[960px]:p-4">
      <div className="grid items-stretch gap-4 md:grid-cols-[minmax(0,1fr)_28px_minmax(0,2.2fr)_28px_minmax(0,1.2fr)]">
        <div className="rounded-xl border border-line p-4">
          <Eyebrow>{repo.repo}</Eyebrow>
          {repoName && <div className="mt-2 font-semibold text-head">{repoName}</div>}
          <p className="m-0 mt-1 text-sm text-muted">{repo.repoNote}</p>
        </div>
        <Arrow />
        <div className="rounded-xl border-2 border-line-strong p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4">
            <span className="font-semibold text-head">{repo.cage}</span>
            <span className="font-mono text-[11px] text-muted">{runtime ? `${runtime} · ` : ""}{repo.cageFacts}</span>
          </div>
          <div className="mt-3 grid gap-2.5 sm:grid-cols-3">
            <div className="rounded-xl border border-line p-3">
              <div className="text-xs text-muted">{repo.agent[0]}</div>
              <div className="font-semibold text-head">{repo.agent[1]}</div>
            </div>
            <div className="rounded-xl border border-ember-border bg-ember-glow p-3">
              <div className="text-xs text-ember">{repo.gate}</div>
              <div className="font-semibold text-head">{counts.checked} checked · {counts.blocked} blocked</div>
            </div>
            <div className="rounded-xl border border-line p-3">
              <div className="text-xs text-muted">{repo.sandbox[0]}</div>
              <div className="font-semibold text-head">{repo.sandbox[1]}</div>
            </div>
          </div>
        </div>
        <Arrow />
        <div className="flex flex-col gap-3">
          <div className="rounded-xl border border-line p-4">
            <Eyebrow>{repo.leaves}</Eyebrow>
            <div className="mt-2 font-semibold text-head">{repo.leavesNote}</div>
          </div>
          {net.hosts.length > 0 && (
            <div className="rounded-xl border border-dashed border-ember-border p-4">
              <Eyebrow ember>{repo.outside}</Eyebrow>
              {net.hosts.map((h) => (
                <div key={h} className="mt-1 break-all font-mono text-sm text-ember line-through">{h}</div>
              ))}
              {net.bytesSent === 0 && <div className="mt-1 text-sm text-muted">{repo.bytes}</div>}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function Arrow() {
  return (
    <svg aria-hidden="true" viewBox="0 0 28 12" className="hidden h-3 w-7 self-center text-line-strong md:block" fill="none" stroke="currentColor" strokeWidth="1.4">
      <path d="M1 6h24m-5-4.5L25 6l-5 4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

type Row = { kind: "step"; step: StepEvent } | { kind: "breaker"; text: string; ts: number };

function workRows(events: Event[]): Row[] {
  const rows: Row[] = [];
  events.forEach((e, index) => {
    if (e.kind === "step") rows.push({ kind: "step", step: e });
    else if (e.kind === "breaker" && e.state === "tripped") {
      const blocked = blockedSinceLastResume(events.slice(0, index));
      const text = breakerText(blocked, e.source).headline;
      const later = events.slice(index + 1).find((x) => x.kind === "breaker" && x.state !== "tripped");
      const tail = later && later.kind === "breaker" ? (later.state === "resumed" ? ` You resumed it at ${timeLabel(later.ts)}.` : " You ended the run here.") : "";
      rows.push({ kind: "breaker", text: `Paused: ${text}${tail}`, ts: e.ts });
    }
  });
  return rows;
}

function WorkList({ events, showAll }: { events: Event[]; showAll: boolean }) {
  const rows = workRows(events);
  const allSteps = stepsOf(events);
  return (
    <>
      <ol className="m-0 list-none overflow-hidden rounded-card border border-line bg-graphite p-0" aria-label={COPY.report.while}>
        {rows.map((row, i) =>
          row.kind === "breaker" ? (
            <li key={`b${i}`} className="flex items-start gap-3 border-b border-line-soft px-5 py-4 last:border-b-0">
              <span aria-hidden="true" className="mt-0.5 font-mono text-ember">॥</span>
              <span className="text-body">{row.text}</span>
            </li>
          ) : row.step.blocked ? (
            <li key={row.step.id} data-blocked="true" className="grid grid-cols-[28px_1fr_auto] gap-x-3 border-b border-ember-line bg-ember-glow px-5 py-4 last:border-b-0">
              <BlockedIcon className="mt-0.5 text-ember" />
              <div className="min-w-0">
                <div className="font-semibold text-ember">{blockedLine(row.step)}</div>
                <div className="mt-1 break-words font-mono text-[13px] text-detail">{actionLine(row.step)}</div>
                <div className="mt-0.5 break-words font-mono text-[13px] text-ember-soft">{ruleLine(row.step)}</div>
              </div>
              <span className="font-mono text-xs text-ember-soft">{stepNumber(row.step.step)}</span>
            </li>
          ) : (
            <li key={row.step.id} className="grid grid-cols-[28px_1fr_auto] gap-x-3 border-b border-line-soft px-5 py-4 last:border-b-0">
              <CheckIcon className="mt-0.5 text-faint" />
              <div className="min-w-0">
                <div className="text-body">
                  {allowedSentence(row.step)}
                  {row.step.decision === "ask" && row.step.approved === true && <span className="ml-2 text-xs text-ember-soft">You allowed this once</span>}
                </div>
                <div className="mt-0.5 break-words font-mono text-[13px] text-detail">
                  {actionLine(row.step)}
                  {row.step.rule_id ? ` · ${row.step.rule_id}` : ""}
                </div>
              </div>
              <span className="font-mono text-xs text-faint">{stepNumber(row.step.step)}</span>
            </li>
          ),
        )}
      </ol>
      {showAll && (
        <div className="sc-scroll mt-4 overflow-x-auto rounded-card border border-line">
          <table className="w-full min-w-[640px] border-collapse text-left text-[13px]">
            <caption className="sr-only">Every action with the decision the gate made</caption>
            <thead>
              <tr className="border-b border-line text-muted">
                {COPY.report.tableHead.map((h) => (
                  <th key={h} scope="col" className="px-4 py-2.5 font-semibold">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {allSteps.map((s) => (
                <tr key={s.id} className={cx("border-b border-line-soft align-top last:border-b-0", s.decision === "deny" && "bg-ember-glow")}>
                  <td className="px-4 py-2.5 font-mono text-faint">{stepNumber(s.step)}</td>
                  <td className="break-words px-4 py-2.5 font-mono text-detail">{actionLine(s)}</td>
                  <td className={cx("px-4 py-2.5 font-semibold", s.decision === "deny" ? "text-ember" : "text-body")}>{s.decision}</td>
                  <td className="px-4 py-2.5 font-mono text-ember-soft">{s.matched_rules.map((m) => `${m.rule_id} (${m.source})`).join(", ") || "none"}</td>
                  <td className="px-4 py-2.5 font-mono text-detail">{s.taint_sources.join(", ") || "none"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function BeforeCard({ events }: { events: Event[] }) {
  const u = understanding(events);
  const flagged = flaggedPaths(events);
  const plan = latestPlan(events);
  const byHuman = planApprovedByHuman(events);
  const line = "grid grid-cols-[110px_1fr] gap-x-6 gap-y-1 border-b border-line-soft py-4 last:border-b-0 max-[960px]:grid-cols-1";
  if (!u && flagged.length === 0 && !plan) return null;
  return (
    <Card className="px-6 py-2 max-[960px]:px-4">
      {u && (
        <div className={line}>
          <div className="font-semibold text-head">Understood</div>
          <div>
            <div className="text-body">{u.task}</div>
            {u.done_criteria.length > 0 && <div className="text-muted">Done when {u.done_criteria.map(inSentence).join("; ")}.</div>}
          </div>
        </div>
      )}
      {flagged.length > 0 && (
        <div className={line}>
          <div className="font-semibold text-head">Scouted</div>
          <div className="text-body">
            Mapped the repo without letting the agent read it. {flagged.length === 1 ? "1 file contains" : `${flagged.length} files contain`} instructions aimed at AI:{" "}
            {flagged.map((f, i) => (
              <span key={f}>
                {i > 0 && ", "}
                <Tag>{f}</Tag>
              </span>
            ))}
            .
          </div>
        </div>
      )}
      {plan && (
        <div className={line}>
          <div className="font-semibold text-head">Planned</div>
          <div className="text-body">
            {plan.risk === "high" && byHuman ? "High risk, so you approved it. " : ""}It may write{" "}
            <Tag>{plan.manifest.write_paths.join(", ") || "nothing"}</Tag>, run <Tag>{plan.manifest.commands.join(", ") || "nothing"}</Tag>, and has{" "}
            {plan.manifest.network.length === 0 ? "no network" : <>network <Tag>{plan.manifest.network.join(", ")}</Tag></>}.
          </div>
        </div>
      )}
    </Card>
  );
}

function AfterCards({ events }: { events: Event[] }) {
  const verify = latestVerify(events);
  const rt = redteam(events);
  const failed = verify?.checks.filter((c) => !c.passed) ?? [];
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {verify && (
        <Card>
          <div className="font-semibold text-head">{verify.passed ? COPY.report.checkPassed : COPY.report.check}</div>
          {verify.passed ? (
            <p className="m-0 mt-1 text-sm text-muted">{COPY.report.checkBody}</p>
          ) : (
            <ul className="m-0 mt-1 list-disc pl-5 text-sm text-muted">
              {failed.map((c) => (
                <li key={c.name}>{c.name}{c.detail ? `: ${c.detail}` : ""}</li>
              ))}
            </ul>
          )}
        </Card>
      )}
      {rt && (
        <Card>
          <div className="font-semibold text-head">
            {rt.unreviewed
              ? "Attacker review: not completed"
              : rt.findings.length === 0
                ? COPY.report.attackerNone
                : `Attacker review: ${rt.findings.length} ${rt.findings.length === 1 ? "finding" : "findings"}`}
          </div>
          {rt.findings.length > 0 ? (
            <ul className="m-0 mt-2 list-none space-y-2 p-0 text-sm text-body">
              {rt.findings.map((f, i) => (
                <li key={i}>
                  <Pill tone={f.severity === "high" || f.severity === "critical" ? "ember" : "outline"}>{f.severity}</Pill>{" "}
                  <span className="font-mono text-fg">{f.line === null ? f.file : `${f.file}:${f.line}`}</span> {f.explanation}
                </li>
              ))}
            </ul>
          ) : (
            <p className="m-0 mt-1 text-sm text-muted">{rt.skipped_reason ?? COPY.report.attackerBody}</p>
          )}
        </Card>
      )}
    </div>
  );
}

export function RunReport({
  runId,
  events,
  info,
  releasedByHuman,
  onReleased,
}: {
  runId: string;
  events: Event[];
  info: RunRow | null;
  releasedByHuman: boolean;
  onReleased: () => void;
}) {
  const done = doneEvent(events);
  const counts = gateCounts(events);
  const result = outcome(events, releasedByHuman);
  const budget = latestBudget(events) ?? done?.budget ?? null;
  const tokens = measuredTokens(budget);
  const u = understanding(events);
  const learn = learnEvent(events);
  const cps = deriveCheckpoints(events);
  const t = trifecta(events);
  const [showAll, setShowAll] = useState(false);
  const [section, setSection] = useState(0);
  const refs = [useRef<HTMLDivElement>(null), useRef<HTMLDivElement>(null), useRef<HTMLDivElement>(null)];
  const [learnStatus, setLearnStatus] = useState<Record<string, LearnStatus | undefined>>({});

  useEffect(() => {
    const scroller = document.querySelector<HTMLElement>(".sc-main");
    const update = (): void => {
      let active = 0;
      refs.forEach((r, i) => {
        const top = r.current?.getBoundingClientRect().top ?? Infinity;
        if (top <= 160) active = i;
      });
      setSection(active);
    };
    scroller?.addEventListener("scroll", update, { passive: true });
    window.addEventListener("scroll", update, { passive: true });
    update();
    return () => {
      scroller?.removeEventListener("scroll", update);
      window.removeEventListener("scroll", update);
    };
    // The refs are stable objects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const goTo = (index: number): void => refs[index]?.current?.scrollIntoView({ behavior: "smooth", block: "start" });

  const addRule = async (id: string): Promise<void> => {
    setLearnStatus((s) => ({ ...s, [id]: "busy" }));
    try {
      await acceptSuggestion(runId, id);
      setLearnStatus((s) => ({ ...s, [id]: "added" }));
    } catch (cause) {
      setLearnStatus((s) => ({ ...s, [id]: cause instanceof ApiError && cause.status === 409 ? "already" : "error" }));
    }
  };

  const repoName = info ? (info.repo.startsWith("bench:") ? `Bench · ${info.repo.slice(6)}` : info.repo) : null;
  const guardKnown = done?.guard ?? info?.guard;
  const modelLabel = done?.provider === "fake" ? "scripted model" : (done?.model ?? info?.model ?? null);
  const showCheckpoints = cps.rows.length > 1 || cps.lastRestore !== null;
  const reportReady = reportEvent(events) !== undefined;

  const sectionLinks = useMemo(() => COPY.report.sections, []);

  return (
    <Body>
      <Main flush>
        <TopBarSlot>
          <span className="font-mono text-[13px] text-muted">Runs / {shortId(runId)}</span>
        </TopBarSlot>
        <div className="mx-auto max-w-[980px] px-10 pt-10 max-[960px]:px-4 max-[960px]:pt-7">
          <div className="flex flex-wrap items-center gap-2" aria-label="Run summary">
            {repoName && <Pill mono>{repoName}</Pill>}
            <Pill mono>run {shortId(runId)}</Pill>
            {modelLabel && <Pill mono>{modelLabel}</Pill>}
            {guardKnown !== undefined && <Pill mono>guard {guardKnown ? "on" : "off"}</Pill>}
            {tokens !== null && <Pill mono>{tokens} tokens</Pill>}
            <Pill tone={result.kind === "withheld" ? "ember" : "solid"} mono>
              {outcomePill(result)}
            </Pill>
          </div>
          <Headline segments={reportHeadline(result, counts.blocked)} />
          <p className="mb-0 mt-4 max-w-[70ch] text-[17px] text-muted" data-testid="subline">
            {reportSubline(result, u?.task ?? info?.task ?? null, counts.checked, done?.summary ?? null)}
          </p>
          <ContainmentMap events={events} repo={repoName} />

        </div>
        <div className="sticky top-0 z-10 mt-8 border-b border-line-soft bg-bar max-[960px]:static" data-testid="section-bar"><div className="mx-auto flex max-w-[980px] flex-wrap items-center justify-between gap-x-6 px-10 py-3 text-sm max-[960px]:px-4">
            <nav aria-label="Report sections" className="flex gap-5">
              {sectionLinks.map((label, i) => (
                <button
                  key={label}
                  type="button"
                  aria-current={section === i ? "true" : undefined}
                  onClick={() => goTo(i)}
                  className={cx("cursor-pointer border-0 bg-transparent p-0 text-sm", section === i ? "font-semibold text-fg" : "text-muted hover:text-fg")}
                >
                  {label}
                </button>
              ))}
            </nav>
            <div className="text-muted">
              {counts.checked} actions{counts.blocked > 0 && <span className="text-ember"> · {counts.blocked} blocked</span>}
              {budget && <span> · {clock(budget.seconds)}</span>}
              {tokens !== null && <span> · {tokens} tokens</span>}
            </div>
          </div>
          </div>
        <div className="mx-auto max-w-[980px] px-10 pb-12 max-[960px]:px-4">
          <div ref={refs[0]} className="scroll-mt-16 pt-8">
            <Eyebrow>{COPY.report.before}</Eyebrow>
            <div className="mt-3">
              <BeforeCard events={events} />
            </div>
          </div>
          <div ref={refs[1]} className="scroll-mt-16 pt-10">
            <Eyebrow>{COPY.report.while}</Eyebrow>
            <div className="mt-3">
              <WorkList events={events} showAll={showAll} />
              <Button className="mt-4" aria-expanded={showAll} onClick={() => setShowAll((v) => !v)}>
                {showAll ? COPY.report.hideAll : COPY.report.showAll}
              </Button>
            </div>
          </div>
          <div ref={refs[2]} className="scroll-mt-16 pt-10">
            <Eyebrow>{COPY.report.after}</Eyebrow>
            <div className="mt-3">
              <AfterCards events={events} />
            </div>
          </div>
        </div>
      </Main>

      <Side label="Report details">
        {hasPatchCard(events) && <DeliverPanel runId={runId} releasedByHuman={result.kind === "released"} onReleased={onReleased} />}
        {t && reportReady && <TrifectaCard legs={trifectaLegs(t, reportEvent(events))} />}
        {t && !reportReady && <TrifectaCard legs={trifectaLegs(t, undefined)} />}
        <ReachedCard tried={triedTargets(events)} changed={changedFiles(events)} />
        {showCheckpoints && <CheckpointsCard checkpoints={cps} />}
        {learn && (
          <LearnCard
            suggestions={learn.suggestions}
            status={learnStatus}
            onAdd={(id) => void addRule(id)}
            onDismiss={(id) => setLearnStatus((s) => ({ ...s, [id]: "dismissed" }))}
          />
        )}
      </Side>
    </Body>
  );
}
