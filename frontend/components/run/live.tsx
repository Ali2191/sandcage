"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Event } from "@contract";
import { stopRun, ApiError, type RunRow } from "@/lib/api";
import { COPY } from "@/lib/copy";
import {
  blockedSteps,
  latestBudget,
  canRewind,
  checkpoints as deriveCheckpoints,
  containmentMoment,
  flaggedPaths,
  gateCounts,
  latestPlan,
  pendingApproval,
  shortId,
  stages,
  steps as stepsOf,
  clock,
} from "@/lib/derive";
import { isTypingTarget } from "@/lib/keys";
import { rewind } from "@/lib/api";
import { Body, Side } from "../shell";
import { Button, Kbd, Pill, cx } from "../ui";
import { TopBarSlot } from "../top-bar-slot";
import { DecisionBanner } from "../decision-banner";
import { useApproval } from "../use-approval";
import { FeedRow } from "./feed-row";
import { BudgetCard, CheckpointsCard, PlanHeldCard, UntrustedCard } from "./cards";
import { StageRow } from "./stage-row";
import { StatusPill, type ConnectionView } from "./status-pill";

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function LiveRun({ runId, events, info, connection }: { runId: string; events: Event[]; info: RunRow | null; connection: ConnectionView }) {
  const now = useNow(1000);
  const pending = pendingApproval(events);
  const { state: approvalState, decide } = useApproval(runId, pending?.approval.id ?? null);
  const allSteps = stepsOf(events);
  const counts = gateCounts(events);
  const flagged = useMemo(() => flaggedPaths(events), [events]);
  const budget = latestBudget(events);
  const plan = latestPlan(events);
  const cps = deriveCheckpoints(events);
  const moment = containmentMoment(events);
  const firstEventTs = events[0]?.ts ?? null;
  const elapsed = firstEventTs === null ? 0 : Math.max(0, now / 1000 - firstEventTs);
  const guard = allSteps.at(-1)?.guard ?? info?.guard ?? true;

  // --- stop ---
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const stop = useCallback(async (): Promise<void> => {
    setStopping(true);
    setStopError(null);
    try {
      await stopRun(runId);
    } catch (cause) {
      setStopping(false);
      setStopError(cause instanceof ApiError ? (cause.needsCode ? COPY.live.stopNeedsCode : cause.message) : COPY.live.sendFailed);
    }
  }, [runId]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape" || isTypingTarget(event.target) || stopping) return;
      void stop();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stop, stopping]);

  // --- rewind ---
  const [rewinding, setRewinding] = useState<number | null>(null);
  const doRewind = (id: number): void => {
    setRewinding(id);
    // Errors are ignored on purpose: the event stream shows what actually happened.
    rewind(runId, id).then(
      () => setRewinding(null),
      () => setRewinding(null),
    );
  };

  // --- follow live ---
  const feedRef = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [atBottom, setAtBottom] = useState(true);
  const lastRows = allSteps.length;
  useLayoutEffect(() => {
    const el = feedRef.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [lastRows, follow, pending?.eventId]);
  const onScroll = (): void => {
    const el = feedRef.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setAtBottom(bottom);
    if (!bottom && follow && el.scrollHeight > el.clientHeight) setFollow(false);
  };
  const jump = (): void => {
    const el = feedRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setFollow(true);
  };

  const stagesNow = stages(events);
  const actReached = stagesNow.some((s) => s.key === "act" && s.state !== "upcoming");
  const blockedCount = blockedSteps(events).length;
  const rewindable = canRewind(events);

  return (
    <Body>
      <main id="main" tabIndex={-1} className="sc-main sc-main-live">
        <TopBarSlot>
          {moment && (
            <span data-testid="prevented" className="hidden min-w-0 max-w-[46ch] truncate text-sm text-ember md:inline" title={moment.sentence}>
              <span className="text-muted">{COPY.live.prevented}: </span>
              {moment.sentence}
            </span>
          )}
          <StatusPill connection={connection} waiting={pending !== null} />
        </TopBarSlot>

        <div className="flex-none px-8 pt-6 max-[960px]:px-4">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-3">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 font-mono text-[13px] text-muted">
              <span data-testid="runline">
                {info ? `${info.repo.startsWith("bench:") ? `Bench · ${info.repo.slice(6)}` : info.repo} · ` : ""}
                {shortId(runId)} · {clock(elapsed)}
              </span>
              {info?.model && <Pill tone="neutral">{info.provider === "fake" ? "scripted model" : info.model}</Pill>}
            </div>
            <StageRow stages={stagesNow} />
            <Button size="sm" busy={stopping} onClick={() => void stop()} className="ml-auto max-[960px]:ml-0" aria-keyshortcuts="Escape">
              {stopping ? COPY.live.stopping : COPY.live.stop}
              {!stopping && <Kbd>Esc</Kbd>}
            </Button>
          </div>
          {stopError && (
            <p role="alert" className="mb-0 mt-3 text-sm text-ember">
              {stopError}
            </p>
          )}
          {pending && (
            <div className="mt-4">
              <DecisionBanner key={pending.approval.id} approval={pending.approval} deadlineMs={pending.deadlineMs} state={approvalState} onDecide={(choice, answer) => void decide(choice, answer)} />
            </div>
          )}
        </div>

        <div className="mt-5 flex min-h-0 flex-1 flex-col">
          <div className="sticky top-14 z-10 flex flex-none items-center justify-between gap-3 border-y border-line-soft bg-bar px-8 py-2.5 max-[960px]:px-4 min-[961px]:top-0">
            <div className="text-sm">
              <span className="font-semibold text-head">{COPY.live.feedTitle}</span>
              <span className="ml-3 text-xs text-muted">{counts.checked} actions</span>
              {blockedCount > 0 && <span className="text-xs text-ember"> · {blockedCount} blocked</span>}
            </div>
            <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
              <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="size-4 accent-[#c8c8c8]" />
              {COPY.live.follow}
            </label>
          </div>
          <div ref={feedRef} onScroll={onScroll} className="sc-feed" data-testid="feed">
            {allSteps.length === 0 ? (
              <p className="m-0 px-8 py-12 text-center text-muted">{COPY.live.empty}</p>
            ) : (
              <ol className="m-0 list-none p-0" aria-label={COPY.live.feedTitle}>
                {allSteps.map((s) => (
                  <FeedRow key={s.id} step={s} flagged={flagged.includes(String(s.args["path"] ?? ""))} flash={moment?.step.id === s.id} />
                ))}
                {pending === null && <li className="px-8 py-4 pl-[120px] text-muted max-[960px]:px-4 max-[960px]:pl-[88px]">{COPY.live.working}</li>}
              </ol>
            )}
          </div>
          {!atBottom && (
            <button
              type="button"
              onClick={jump}
              className="absolute bottom-6 left-1/2 z-20 -translate-x-1/2 cursor-pointer rounded-full border border-line-strong bg-raised px-4 py-1.5 text-sm text-fg shadow-lg hover:border-fg max-[960px]:hidden"
            >
              {COPY.live.jump}
            </button>
          )}
        </div>
      </main>

      <Side label="Run details">
        {budget && <BudgetCard budget={budget} />}
        {plan && <PlanHeldCard plan={plan} guard={guard} sandbox={null} />}
        {flagged.length > 0 && <UntrustedCard paths={flagged} />}
        {actReached && <CheckpointsCard checkpoints={cps} onRewind={rewindable ? doRewind : undefined} rewinding={rewinding} />}
      </Side>
    </Body>
  );
}
export { cx };
